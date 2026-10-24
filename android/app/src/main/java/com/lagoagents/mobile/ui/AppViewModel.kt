package com.lagoagents.mobile.ui

import android.app.Application
import android.os.Build
import android.os.SystemClock
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.lagoagents.mobile.alerts.Alerts
import com.lagoagents.mobile.data.AppSettings
import com.lagoagents.mobile.data.Profile
import com.lagoagents.mobile.data.Session
import com.lagoagents.mobile.net.LagoClient
import com.lagoagents.mobile.net.LagoException
import com.lagoagents.mobile.net.PairingLink
import com.lagoagents.mobile.terminal.Ansi
import com.lagoagents.mobile.terminal.TerminalLine
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/** Which screen the app shows. */
sealed interface Screen {
    data object Pair : Screen
    data class Login(val profile: Profile) : Screen
    data object Locked : Screen
    data object Home : Screen
}

/** A mirrored terminal: the screen of a tmux session on your computer. */
data class TerminalState(
    val sessionId: String,
    val lines: List<TerminalLine> = emptyList(),
    val running: Boolean = true,
    val loaded: Boolean = false,
    val error: String? = null,
    val expanded: Boolean = false,
    val stamp: Long = 0,
)

class AppViewModel(app: Application) : AndroidViewModel(app) {
    private val context get() = getApplication<Application>()
    val settings = AppSettings.get(app)

    private val _screen = MutableStateFlow(initialScreen())
    val screen: StateFlow<Screen> = _screen.asStateFlow()

    private val _busy = MutableStateFlow(false)
    val busy: StateFlow<Boolean> = _busy.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    /** Shown over the pages when the computer stops answering. */
    private val _offline = MutableStateFlow<String?>(null)
    val offline: StateFlow<String?> = _offline.asStateFlow()

    private val _terminal = MutableStateFlow<TerminalState?>(null)
    val terminal: StateFlow<TerminalState?> = _terminal.asStateFlow()

    private val _settingsOpen = MutableStateFlow(false)
    val settingsOpen: StateFlow<Boolean> = _settingsOpen.asStateFlow()

    /** A page to open (from a tapped notification), consumed by the WebView. */
    private val _navigate = MutableStateFlow<String?>(null)
    val navigate: StateFlow<String?> = _navigate.asStateFlow()

    /** Called when a mirrored session ends, so the page can drop its "in the terminal" bar. */
    var onTerminalEnded: ((String) -> Unit)? = null

    private var backgroundedAt = 0L
    private var skipNextLock = false
    private var terminalJob: Job? = null
    private var foreground = false
    private var fitted: Pair<String, Int>? = null
    var terminalColumns = 0
        private set

    val profile: Profile? get() = Session.profile(context)
    fun client(): LagoClient? = Session.client(context)

    private fun initialScreen(): Screen {
        val p = Session.profile(context) ?: return Screen.Pair
        if (p.token == null) return Screen.Login(p)
        return if (settings.value.lockEnabled) Screen.Locked else Screen.Home
    }

    fun clearError() { _error.value = null }

    // ------------------------------------------------------------ pairing & sign-in

    fun pair(link: PairingLink, password: String) = work {
        val name = deviceName()
        val (fingerprint, cred) = LagoClient.pair(link.url, link.fingerprint, link.code, password, name)
        Session.save(context, Profile(link.url, fingerprint, cred.deviceId, cred.deviceKey, cred.name, cred.token))
        settings.update { it }  // keep defaults
        Alerts.reschedule(context)
        _screen.value = Screen.Home
    }

    fun login(password: String) = work {
        val p = Session.profile(context) ?: run { _screen.value = Screen.Pair; return@work }
        try {
            val token = LagoClient.login(p.serverUrl, p.fingerprint, p.deviceId, p.deviceKey, password)
            Session.save(context, p.copy(token = token))
            _offline.value = null
            Alerts.reschedule(context)
            _screen.value = Screen.Home
        } catch (gone: LagoException.Unpaired) {
            Session.save(context, null)
            _screen.value = Screen.Pair
            throw gone
        }
    }

    /** The page or a background call got a 401: the session lapsed or was ended. */
    fun signedOut() {
        if (_screen.value is Screen.Login || _screen.value is Screen.Pair) return
        Session.dropToken(context)
        closeTerminal(notifyPage = false)
        Session.profile(context)?.let { _screen.value = Screen.Login(it) }
    }

    fun signOut() = viewModelScope.launch(Dispatchers.IO) {
        client()?.logout()
        withContext(Dispatchers.Main) {
            _settingsOpen.value = false
            signedOut()
        }
    }

    /** Forget this computer entirely (it also stops knowing this phone's session). */
    fun unpair() = viewModelScope.launch(Dispatchers.IO) {
        client()?.logout()
        Session.save(context, null)
        settings.clearAlerts()
        Alerts.reschedule(context)
        withContext(Dispatchers.Main) {
            _settingsOpen.value = false
            closeTerminal(notifyPage = false)
            _screen.value = Screen.Pair
        }
    }

    private fun work(block: suspend () -> Unit) {
        if (_busy.value) return
        _busy.value = true
        _error.value = null
        viewModelScope.launch {
            try {
                withContext(Dispatchers.IO) { block() }
            } catch (error: LagoException) {
                _error.value = error.message
            } catch (error: Exception) {
                _error.value = error.message ?: error.javaClass.simpleName
            } finally {
                _busy.value = false
            }
        }
    }

    private fun deviceName(): String {
        val maker = Build.MANUFACTURER.replaceFirstChar { it.uppercase() }
        return if (Build.MODEL.startsWith(Build.MANUFACTURER, ignoreCase = true)) Build.MODEL else "$maker ${Build.MODEL}"
    }

    // ------------------------------------------------------------ app lock

    fun unlocked() {
        if (_screen.value == Screen.Locked) _screen.value = Screen.Home
    }

    /** The next trip away (a file picker, the QR scanner) shouldn't lock the app. */
    fun expectExternal() { skipNextLock = true }

    fun onStop() {
        foreground = false
        backgroundedAt = SystemClock.elapsedRealtime()
    }

    fun onStart() {
        foreground = true
        val prefs = settings.value
        val away = SystemClock.elapsedRealtime() - backgroundedAt
        if (skipNextLock) {
            skipNextLock = false
        } else if (backgroundedAt > 0 && prefs.lockEnabled && away >= prefs.lockAfterMs && _screen.value == Screen.Home) {
            _screen.value = Screen.Locked
        }
        _terminal.value?.let { startPolling() }
    }

    val isForeground get() = foreground

    // ------------------------------------------------------------ connection

    fun reportOffline(message: String?) { _offline.value = message }

    fun openSettings(open: Boolean) { _settingsOpen.value = open }

    fun navigateTo(href: String?) { _navigate.value = href }

    fun navigated() { _navigate.value = null }

    // ------------------------------------------------------------ mirrored terminals

    fun openTerminal(sessionId: String) {
        if (_terminal.value?.sessionId == sessionId) return
        closeTerminal(notifyPage = false)
        _terminal.value = TerminalState(sessionId)
        startPolling()
    }

    fun closeTerminal(notifyPage: Boolean = false) {
        val current = _terminal.value ?: return
        terminalJob?.cancel()
        terminalJob = null
        _terminal.value = null
        unfit(current.sessionId)
        if (notifyPage) onTerminalEnded?.invoke(current.sessionId)
    }

    fun toggleTerminalExpanded() = _terminal.update { it?.copy(expanded = !it.expanded) }

    /** The panel measured how many characters fit across: narrow the session to that. */
    fun setTerminalColumns(columns: Int) {
        terminalColumns = columns
        val state = _terminal.value ?: return
        fit(state.sessionId)
    }

    private fun fit(sessionId: String) {
        if (!settings.value.terminalFit || terminalColumns < 30) return
        if (fitted == sessionId to terminalColumns) return
        fitted = sessionId to terminalColumns
        background {
            it.call("terminal_fit", JSONObject().put("agent_id", sessionId).put("cols", terminalColumns).put("rows", 50))
        }
    }

    private fun unfit(sessionId: String) {
        if (fitted?.first != sessionId) return
        fitted = null
        background { it.call("terminal_unfit", JSONObject().put("agent_id", sessionId)) }
    }

    fun setTerminalFit(on: Boolean) {
        settings.update { it.copy(terminalFit = on) }
        val state = _terminal.value ?: return
        if (on) fit(state.sessionId) else unfit(state.sessionId)
    }

    private fun startPolling() {
        terminalJob?.cancel()
        terminalJob = viewModelScope.launch {
            while (isActive) {
                val state = _terminal.value ?: break
                if (!foreground) break
                val result = withContext(Dispatchers.IO) {
                    runCatching {
                        client()?.call("terminal_snapshot", JSONObject().put("agent_id", state.sessionId).put("lines", 400))
                    }
                }
                val current = _terminal.value ?: break
                if (current.sessionId != state.sessionId) continue
                result.onSuccess { data ->
                    if (data == null) return@onSuccess
                    if (!data.optBoolean("running")) {
                        // The session ended (a login you finished, an agent that stopped).
                        closeTerminal(notifyPage = true)
                        return@launch
                    }
                    fit(state.sessionId)
                    _terminal.value = current.copy(
                        lines = Ansi.parse(data.optString("text")), running = true, loaded = true,
                        error = null, stamp = SystemClock.elapsedRealtime(),
                    )
                }.onFailure { error ->
                    if (error is LagoException.SignedOut) { signedOut(); return@launch }
                    _terminal.value = current.copy(error = error.message, loaded = true)
                }
                // Poll faster right after you pressed something, to show the answer quickly.
                delay(if (SystemClock.elapsedRealtime() - lastInput < 3000) 400 else 1000)
            }
        }
    }

    @Volatile private var lastInput = 0L

    fun terminalSend(text: String) {
        val id = _terminal.value?.sessionId ?: return
        lastInput = SystemClock.elapsedRealtime()
        background { it.call("terminal_send", JSONObject().put("agent_id", id).put("text", text).put("enter", true)) }
    }

    fun terminalKey(key: String) {
        val id = _terminal.value?.sessionId ?: return
        lastInput = SystemClock.elapsedRealtime()
        background { it.call("terminal_key", JSONObject().put("agent_id", id).put("key", key)) }
    }

    private fun background(block: (LagoClient) -> Unit) {
        viewModelScope.launch(Dispatchers.IO) {
            val client = client() ?: return@launch
            try {
                block(client)
            } catch (error: LagoException.SignedOut) {
                withContext(Dispatchers.Main) { signedOut() }
            } catch (error: Exception) {
                _error.value = error.message
            }
        }
    }

    override fun onCleared() {
        _terminal.value?.let { unfit(it.sessionId) }
    }
}
