package com.lagoagents.mobile.call

import android.Manifest
import android.app.KeyguardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_STRONG
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_WEAK
import androidx.biometric.BiometricManager.Authenticators.DEVICE_CREDENTIAL
import androidx.biometric.BiometricPrompt
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.core.view.WindowCompat
import androidx.fragment.app.FragmentActivity
import com.lagoagents.mobile.data.AppSettings
import com.lagoagents.mobile.ui.LagoTheme
import kotlinx.coroutines.delay
import org.json.JSONObject

/**
 * The call screen: Lago ringing you (answer or decline, over the lock screen too), or the
 * call itself (CallService runs it; this shows it and holds the buttons). Talking to Lago
 * can approve and change your agents' work, so connecting a call needs you: a locked phone
 * is unlocked first, and the app's own lock asks as it does when the app opens.
 */
class CallActivity : FragmentActivity() {
    /** Lago's incoming call, until it's answered or declined. */
    private var incoming by mutableStateOf<JSONObject?>(null)
    private var problem by mutableStateOf<String?>(null)
    private var pending: (() -> Unit)? = null

    private val askMic = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) pending?.invoke() else problem = "Allow the microphone to talk to Lago on a call"
        pending = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON)
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        if (AppSettings.get(this).value.secureScreen) window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        if (savedInstanceState == null) take(intent)  // not again after a rotation
        WindowCompat.getInsetsController(window, window.decorView).apply {
            isAppearanceLightStatusBars = false
            isAppearanceLightNavigationBars = false
        }
        setContent { LagoTheme { Screen() } }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        take(intent)
    }

    private fun take(intent: Intent) {
        val call = intent.getStringExtra(Ringer.EXTRA_CALL)?.let { runCatching { JSONObject(it) }.getOrNull() }
        when {
            call != null && intent.getBooleanExtra(EXTRA_ANSWER_NOW, false) -> { incoming = call; answer(call) }
            call != null -> incoming = call
            !CallService.onCall -> connect(trusted = intent.getBooleanExtra(EXTRA_TRUSTED, false)) { CallService.start(this) }
        }
    }

    private fun answer(call: JSONObject) {
        val id = call.optString("id")
        Ringer.answer(this, id)
        connect(trusted = false) {
            incoming = null
            CallService.start(this, answering = id)
        }
    }

    private fun decline(call: JSONObject) {
        Ringer.decline(this, call.optString("id"))
        finish()
    }

    /** Unlock what's locked, get the microphone, then `go`. */
    private fun connect(trusted: Boolean, go: () -> Unit) {
        val withMic = {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) go()
            else { pending = go; askMic.launch(Manifest.permission.RECORD_AUDIO) }
        }
        val keyguard = getSystemService(KeyguardManager::class.java)
        if (keyguard.isKeyguardLocked && keyguard.isDeviceSecure) {
            // Unlocking the phone is the check; the app's own lock doesn't ask again.
            keyguard.requestDismissKeyguard(this, object : KeyguardManager.KeyguardDismissCallback() {
                override fun onDismissSucceeded() = withMic()
                override fun onDismissCancelled() { problem = "Unlock your phone to take the call" }
                override fun onDismissError() { problem = "Unlock your phone to take the call" }
            })
            return
        }
        val prefs = AppSettings.get(this).value
        val authenticators = BIOMETRIC_STRONG or BIOMETRIC_WEAK or DEVICE_CREDENTIAL
        if (trusted || !prefs.lockEnabled ||
            BiometricManager.from(this).canAuthenticate(authenticators) != BiometricManager.BIOMETRIC_SUCCESS) {
            withMic()
            return
        }
        BiometricPrompt(this, ContextCompat.getMainExecutor(this), object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) = withMic()
            override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                problem = "Unlock Lago Agents to take the call"
            }
        }).authenticate(BiometricPrompt.PromptInfo.Builder()
            .setTitle("Unlock Lago Agents")
            .setSubtitle("Lago can change your agents' work on a call")
            .setAllowedAuthenticators(authenticators)
            .build())
    }

    @Composable
    private fun Screen() {
        val ui by CallService.state.collectAsState()
        val ringing = incoming
        LaunchedEffect(ui.phase, ringing) {
            if (ringing == null && ui.phase == "ended" && CallService.state.value.id != null) {
                delay(1500)
                finish()
            }
        }
        // It stops ringing when it's no longer offered (rang out, or dealt with at the desk).
        LaunchedEffect(ringing) {
            while (ringing != null) {
                delay(1000)
                val now = Ringer.ringing
                if (now == null || now.optString("id") != ringing.optString("id")) {
                    if (incoming === ringing) finish()
                    break
                }
            }
        }
        CallScreen(ui, ringing, problem,
            onAnswer = { ringing?.let(::answer) },
            onDecline = { ringing?.let(::decline) },
            onClose = { finish() },
            onMute = { CallService.toggleMute(this) },
            onSpeaker = { CallService.toggleSpeaker(this) },
            onInterrupt = { CallService.interrupt(this) },
            onHangUp = { CallService.hangUp(this) },
            onTalkTo = { CallService.talkTo(this, it) })
    }

    companion object {
        const val EXTRA_ANSWER_NOW = "answer_now"
        /** Started from inside the unlocked app: its lock has already asked. */
        const val EXTRA_TRUSTED = "trusted"
    }
}
