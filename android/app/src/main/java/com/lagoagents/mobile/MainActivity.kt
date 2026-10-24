package com.lagoagents.mobile

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_STRONG
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_WEAK
import androidx.biometric.BiometricManager.Authenticators.DEVICE_CREDENTIAL
import androidx.biometric.BiometricPrompt
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import com.lagoagents.mobile.alerts.Alerts
import com.lagoagents.mobile.alerts.BatteryExemption
import com.lagoagents.mobile.net.PairingLink
import com.lagoagents.mobile.ui.AppViewModel
import com.lagoagents.mobile.ui.HomeScreen
import com.lagoagents.mobile.ui.LagoTheme
import com.lagoagents.mobile.ui.LockScreen
import com.lagoagents.mobile.ui.LoginScreen
import com.lagoagents.mobile.ui.PairScreen
import com.lagoagents.mobile.ui.Screen
import com.lagoagents.mobile.ui.SettingsScreen
import com.lagoagents.mobile.update.Updater
import com.lagoagents.mobile.web.LagoWebView
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.launch

class MainActivity : FragmentActivity() {
    private val vm: AppViewModel by viewModels()
    private var web: LagoWebView? = null
    private var webLoaded = false

    private var scanned by mutableStateOf<PairingLink?>(null)
    private var scanError by mutableStateOf<String?>(null)
    private var lockError by mutableStateOf<String?>(null)

    private var fileCallback: ((Array<Uri>?) -> Unit)? = null
    private var micAnswer: CompletableDeferred<Boolean>? = null
    private var updateRequested = false

    private val pickFiles = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        fileCallback?.invoke(uris.takeIf { it.isNotEmpty() }?.toTypedArray())
        fileCallback = null
    }

    private val scanQr = registerForActivityResult(ScanContract()) { result ->
        val text = result.contents ?: return@registerForActivityResult
        val link = PairingLink.parse(text)
        scanned = link
        scanError = if (link == null) "That isn't a Lago Agents pairing code. Open Phone in Lago Agents on your computer." else null
    }

    private val micPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        micAnswer?.complete(granted)
        micAnswer = null
    }

    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) {
        askForBattery()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        intent?.getStringExtra(Alerts.EXTRA_HREF)?.let { vm.navigateTo(it) }
        updateRequested = intent?.getBooleanExtra(Updater.EXTRA_UPDATE, false) == true

        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                vm.settings.flow.collect { prefs ->
                    if (prefs.secureScreen) window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
                    else window.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
                }
            }
        }

        setContent {
            LagoTheme {
                val screen by vm.screen.collectAsStateWithLifecycle()
                val busy by vm.busy.collectAsStateWithLifecycle()
                val error by vm.error.collectAsStateWithLifecycle()
                val prefs by vm.settings.flow.collectAsStateWithLifecycle()
                val settingsOpen by vm.settingsOpen.collectAsStateWithLifecycle()
                val update by Updater.state.collectAsStateWithLifecycle()

                when (val s = screen) {
                    Screen.Pair -> PairScreen(
                        busy = busy, error = scanError ?: error, scanned = scanned,
                        onScan = ::startScan,
                        onPair = { link, password -> scanError = null; vm.pair(link, password) },
                    )
                    is Screen.Login -> LoginScreen(
                        profile = s.profile, busy = busy, error = error,
                        onLogin = vm::login, onForget = { vm.unpair() },
                    )
                    Screen.Locked -> LockScreen(
                        available = lockAvailable(), error = lockError,
                        onUnlock = ::unlock, onUsePassword = { vm.signedOut() },
                    )
                    Screen.Home -> {
                        val webView = remember_web()
                        val terminal by vm.terminal.collectAsStateWithLifecycle()
                        val offline by vm.offline.collectAsStateWithLifecycle()
                        val navigate by vm.navigate.collectAsStateWithLifecycle()
                        LaunchedEffect(Unit) {
                            scanned = null
                            if (!webLoaded) {
                                webView.load(navigate)
                                vm.navigated()
                                webLoaded = true
                            }
                            Alerts.reschedule(this@MainActivity)  // live alerts on, now that we're signed in
                            checkForUpdate()
                            if (updateRequested) { updateRequested = false; Updater.start(this@MainActivity, foreground = true) }
                            askForNotifications()
                        }
                        LaunchedEffect(navigate) {
                            navigate?.let { webView.navigate(it); vm.navigated() }
                        }
                        if (settingsOpen) {
                            SettingsScreen(
                                profile = vm.profile, prefs = prefs, lockAvailable = lockAvailable(),
                                onPrefs = { vm.settings.update(it) },
                                onAlertsChanged = { Alerts.reschedule(this) },
                                backgroundAllowed = BatteryExemption.granted(this),
                                onAllowBackground = { vm.expectExternal(); BatteryExemption.request(this) },
                                update = update,
                                onUpdate = { Updater.start(this, foreground = true) },
                                onCheckUpdate = ::checkForUpdate,
                                onSignOut = { vm.signOut() },
                                onUnpair = { vm.unpair() },
                                onClose = { vm.openSettings(false) },
                            )
                        } else {
                            HomeScreen(
                                webView = webView.view, terminal = terminal, prefs = prefs,
                                offline = offline, error = error,
                                onRetry = { vm.reportOffline(null); webView.reload() },
                                onErrorShown = vm::clearError,
                                onBack = ::goBack,
                                update = update,
                                onUpdate = { Updater.start(this, foreground = true) },
                                vm = vm,
                            )
                        }
                    }
                }
                LaunchedEffect(screen) {
                    // Leaving the app proper (signed out, unpaired): the next visit starts the pages afresh.
                    if (screen !is Screen.Home && screen != Screen.Locked) webLoaded = false
                }
            }
        }
    }

    @Suppress("FunctionName")
    private fun remember_web(): LagoWebView = web ?: LagoWebView(
        this, vm,
        pickFiles = { types, callback ->
            fileCallback?.invoke(null)
            fileCallback = callback
            pickFiles.launch(types)
        },
        askMic = ::askMic,
    ).also {
        it.setTheme(isDark())
        web = it
    }

    private fun goBack() {
        val terminal = vm.terminal.value
        val view = web?.view
        when {
            terminal?.expanded == true -> vm.toggleTerminalExpanded()
            terminal != null -> vm.closeTerminal()
            view?.canGoBack() == true -> view.goBack()
            else -> moveTaskToBack(true)
        }
    }

    private fun startScan() {
        vm.expectExternal()
        scanQr.launch(
            ScanOptions()
                .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                .setPrompt("Scan the code on your computer's Phone page")
                .setBeepEnabled(false)
                .setOrientationLocked(false)
        )
    }

    private suspend fun askMic(): Boolean {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) return true
        val answer = CompletableDeferred<Boolean>()
        runOnUiThread {
            micAnswer?.complete(false)
            micAnswer = answer
            vm.expectExternal()
            micPermission.launch(Manifest.permission.RECORD_AUDIO)
        }
        return answer.await()
    }

    /** Once, after pairing: notifications, then running in the background (for live alerts). */
    private fun askForNotifications() {
        val prefs = getSharedPreferences("lago_prefs", MODE_PRIVATE)
        val needed = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        if (!needed || prefs.getBoolean("askedNotifications", false)) {
            askForBattery()
            return
        }
        prefs.edit().putBoolean("askedNotifications", true).apply()
        vm.expectExternal()
        notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    private fun askForBattery() {
        val prefs = getSharedPreferences("lago_prefs", MODE_PRIVATE)
        if (!vm.settings.value.instantAlerts || BatteryExemption.granted(this) || prefs.getBoolean("askedBattery", false)) return
        prefs.edit().putBoolean("askedBattery", true).apply()
        vm.expectExternal()
        BatteryExemption.request(this)
    }

    // ------------------------------------------------------------ app lock

    private val authenticators
        get() = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) BIOMETRIC_STRONG or DEVICE_CREDENTIAL
        else BIOMETRIC_WEAK or DEVICE_CREDENTIAL

    private fun lockAvailable() =
        BiometricManager.from(this).canAuthenticate(authenticators) == BiometricManager.BIOMETRIC_SUCCESS

    private fun unlock() {
        if (!lockAvailable()) return
        lockError = null
        val prompt = BiometricPrompt(this, ContextCompat.getMainExecutor(this), object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                vm.unlocked()
            }

            override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                if (errorCode != BiometricPrompt.ERROR_USER_CANCELED && errorCode != BiometricPrompt.ERROR_CANCELED &&
                    errorCode != BiometricPrompt.ERROR_NEGATIVE_BUTTON) lockError = errString.toString()
            }
        })
        prompt.authenticate(
            BiometricPrompt.PromptInfo.Builder()
                .setTitle("Unlock Lago Agents")
                .setSubtitle("Your agents and their terminals are behind this lock")
                .setAllowedAuthenticators(authenticators)
                .build()
        )
    }

    // ------------------------------------------------------------ lifecycle

    override fun onStart() {
        super.onStart()
        vm.onStart()
        if (vm.screen.value == Screen.Home) {
            Alerts.reschedule(this)  // reconnects live alerts now if they were waiting
            Updater.onForeground(this)
        }
    }

    override fun onStop() {
        vm.onStop()
        super.onStop()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        intent.getStringExtra(Alerts.EXTRA_HREF)?.let { vm.navigateTo(it) }
        if (intent.getBooleanExtra(Updater.EXTRA_UPDATE, false)) {
            if (vm.screen.value == Screen.Home) Updater.start(this, foreground = true) else updateRequested = true
        }
    }

    /** Ask the computer for a newer version (the live feed also announces them). */
    private fun checkForUpdate() {
        lifecycleScope.launch(kotlinx.coroutines.Dispatchers.IO) { runCatching { Updater.check(this@MainActivity) } }
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        web?.setTheme(isDark())
    }

    private fun isDark() =
        (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES

    override fun onDestroy() {
        web?.destroy()
        web = null
        super.onDestroy()
    }
}
