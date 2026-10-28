package com.lagoagents.mobile.call

import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.os.PowerManager
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.lagoagents.mobile.R
import com.lagoagents.mobile.alerts.Alerts
import com.lagoagents.mobile.alerts.LiveAlertsService
import com.lagoagents.mobile.data.Session
import com.lagoagents.mobile.voice.SpeechPlayer
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/** What the call screen shows. */
data class CallUi(
    val id: String? = null,
    val persona: String = "Lago",
    /** "lago", "chip", "marina"... (personas.py): whose face the call screen shows. */
    val personaId: String = "lago",
    /** The language to listen in, a BCP 47 tag ("pt-PT"), or "" for the phone's own. */
    val language: String = "",
    val figure: String = "",
    /** "connecting", "listening", "thinking", "speaking" or "ended". */
    val phase: String = "connecting",
    val heard: String = "",
    /** Why he's quiet, when he is for long: "Claude's servers are overloaded…". */
    val note: String? = null,
    val muted: Boolean = false,
    val speaker: Boolean = true,
    val error: String? = null,
    /** When the call connected (elapsedRealtime), for the call screen's timer. */
    val since: Long? = null,
)

/**
 * A phone call with Lago (calls.py on the computer). Runs as a foreground service so the call
 * goes on with the screen off or locked. Hands-free: Android's speech recognition listens,
 * and each phrase you finish goes to Lago (`call_say`); his spoken replies, in his own voice
 * made on the computer, arrive through the live connection and [SpeechPlayer] plays them.
 * It doesn't listen while he talks, so it never hears itself, and the recognizer's beeps are
 * muted for the call (his voice plays as a call, on a stream that stays on). Every few seconds it asks the
 * computer how the call stands (`call_state`): who's on it, whether he's thinking or talking,
 * and whether the call has ended there.
 */
class CallService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var recognizer: SpeechRecognizer? = null
    private var listening = false
    private var serverSpeaking = false
    private var lagoStatus = ""
    private var poll: Job? = null
    private var proximity: PowerManager.WakeLock? = null
    private var beepsMuted = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_HANG_UP -> { hangUp(); return START_NOT_STICKY }
            ACTION_MUTE -> { setMuted(!state.value.muted); return START_NOT_STICKY }
            ACTION_SPEAKER -> { setSpeaker(!state.value.speaker); return START_NOT_STICKY }
            ACTION_INTERRUPT -> { interrupt(); return START_NOT_STICKY }
            ACTION_SAY -> { intent.getStringExtra(EXTRA_TEXT)?.let(::send); return START_NOT_STICKY }
        }
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0
        ServiceCompat.startForeground(this, NOTIFICATION_ID, notification(), type)
        if (poll?.isActive == true) return START_NOT_STICKY
        running = this
        _state.value = CallUi()
        val answering = intent?.getStringExtra(EXTRA_ANSWER)
        LiveAlertsService.start(this)  // Lago's voice comes through the live connection
        SpeechPlayer.callAudio = true
        routeAudio(speaker = true)
        muteBeeps(true)
        poll = scope.launch {
            val client = Session.client(applicationContext)
            if (client == null) { end("Sign in to Lago Agents first"); return@launch }
            val call = try {
                if (answering != null) client.call("call_answer", JSONObject().put("id", answering)).getJSONObject("call")
                else client.call("call_start").getJSONObject("call")
            } catch (error: Exception) {
                end(error.message ?: "Can't reach your computer"); return@launch
            }
            show(call)
            _state.update { it.copy(since = android.os.SystemClock.elapsedRealtime()) }
            withContext(Dispatchers.Main) { listen() }
            launch { SpeechPlayer.busy.collect { withContext(Dispatchers.Main) { refresh() } } }
            var failures = 0
            while (isActive) {
                delay(POLL_MS)
                val id = state.value.id ?: break
                try {
                    val answer = client.call("call_state", JSONObject().put("id", id))
                    failures = 0
                    val now = answer.optJSONObject("call")
                    if (now == null) { end(null); break }
                    show(now)
                    lagoStatus = answer.optString("lago")
                    val note = if (answer.isNull("note")) null else answer.optString("note").ifBlank { null }
                    if (note != state.value.note) _state.update { it.copy(note = note) }
                    serverSpeaking = answer.optBoolean("speaking")
                    withContext(Dispatchers.Main) { refresh() }
                } catch (error: Exception) {
                    if (++failures * POLL_MS > LOST_MS) { end("Lost the connection to your computer"); break }
                }
            }
        }
        return START_NOT_STICKY
    }

    private fun show(call: JSONObject) {
        val persona = call.optJSONObject("persona")
        _state.update {
            it.copy(id = call.optString("id", it.id), persona = persona?.optString("name") ?: it.persona,
                personaId = persona?.optString("id")?.ifBlank { null } ?: it.personaId,
                figure = persona?.optString("figure") ?: it.figure,
                language = if (call.has("language")) call.optString("language") else it.language)
        }
    }

    // ------------------------------------------------------------ listening

    /** Listen unless muted or while he's talking (or about to): the recognizer would hear him. */
    private fun refresh() {
        val talking = SpeechPlayer.busy.value || serverSpeaking
        val ui = state.value
        if (ui.phase == "ended") return
        val wantEars = !ui.muted && !talking
        if (wantEars && !listening) listen() else if (!wantEars && listening) stopListening()
        val phase = when {
            talking -> "speaking"
            lagoStatus in THINKING -> "thinking"
            else -> "listening"
        }
        if (phase != ui.phase) _state.update { it.copy(phase = phase) }
    }

    private fun listen() {
        if (state.value.muted || state.value.phase == "ended") return
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            _state.update { it.copy(error = "This phone has no speech recognition") }
            return
        }
        if (recognizer == null) recognizer = SpeechRecognizer.createSpeechRecognizer(this).apply { setRecognitionListener(ears) }
        listening = true
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 1500L)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 1200L)
            // He talks in another language (languages.py): listen in it. Otherwise, in the
            // phone's own language.
            val language = state.value.language
            if (language.isNotEmpty()) putExtra(RecognizerIntent.EXTRA_LANGUAGE, language)
        }
        runCatching { recognizer?.startListening(intent) }
    }

    private fun stopListening() {
        listening = false
        runCatching { recognizer?.cancel() }
    }

    private fun send(text: String) {
        val id = state.value.id ?: return
        _state.update { it.copy(heard = text, phase = "thinking") }
        scope.launch {
            try {
                Session.client(applicationContext)?.call("call_say", JSONObject().put("id", id).put("text", text))?.optJSONObject("call")?.let(::show)
            } catch (error: Exception) {
                _state.update { it.copy(error = error.message) }
            }
        }
    }

    private val ears = object : RecognitionListener {
        override fun onResults(results: Bundle?) {
            val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty().trim()
            if (text.isNotEmpty() && listening) send(text)
            if (listening) listen()
        }

        override fun onPartialResults(partialResults: Bundle?) {
            val text = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty()
            if (text.isNotBlank()) _state.update { it.copy(heard = text) }
        }

        override fun onError(error: Int) {
            if (!listening) return
            if (error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS) {
                listening = false
                _state.update { it.copy(error = "Allow the microphone for Lago Agents") }
                return
            }
            // A pause with nothing said, or the recognizer busy or timing out: listen again.
            scope.launch(Dispatchers.Main) {
                if (error == SpeechRecognizer.ERROR_RECOGNIZER_BUSY || error == SpeechRecognizer.ERROR_CLIENT) delay(400)
                if (listening) listen()
            }
        }

        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    // ------------------------------------------------------------ controls

    private fun setMuted(on: Boolean) {
        _state.update { it.copy(muted = on) }
        refresh()
        updateNotification()
    }

    /** Stop him mid-sentence and listen. */
    private fun interrupt() {
        SpeechPlayer.stop()
        serverSpeaking = false
        scope.launch { runCatching { Session.client(applicationContext)?.call("call_hush") } }
        refresh()
    }

    private fun setSpeaker(on: Boolean) {
        _state.update { it.copy(speaker = on) }
        routeAudio(on)
        updateNotification()
    }

    /** The speaker, or the earpiece with the screen going dark at your ear. */
    private fun routeAudio(speaker: Boolean) {
        val audio = getSystemService(AudioManager::class.java)
        audio.mode = AudioManager.MODE_IN_COMMUNICATION
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val wanted = if (speaker) AudioDeviceInfo.TYPE_BUILTIN_SPEAKER else AudioDeviceInfo.TYPE_BUILTIN_EARPIECE
            // A headset or car speaker, when there is one, wins over both.
            val headset = audio.availableCommunicationDevices.firstOrNull { it.type in HEADSETS }
            val device = headset ?: audio.availableCommunicationDevices.firstOrNull { it.type == wanted }
            if (device != null) audio.setCommunicationDevice(device)
        } else {
            @Suppress("DEPRECATION")
            audio.isSpeakerphoneOn = speaker
        }
        val power = getSystemService(PowerManager::class.java)
        if (!speaker && proximity == null && power.isWakeLockLevelSupported(PowerManager.PROXIMITY_SCREEN_OFF_WAKE_LOCK)) {
            proximity = power.newWakeLock(PowerManager.PROXIMITY_SCREEN_OFF_WAKE_LOCK, "lago:call").apply { acquire(4 * 60 * 60_000L) }
        } else if (speaker) {
            proximity?.let { if (it.isHeld) it.release() }
            proximity = null
        }
    }

    /** Android's recognizer beeps each time it starts listening: not on a call. */
    private fun muteBeeps(on: Boolean) {
        if (on == beepsMuted) return
        beepsMuted = on
        val audio = getSystemService(AudioManager::class.java)
        for (stream in BEEP_STREAMS) {
            runCatching { audio.adjustStreamVolume(stream, if (on) AudioManager.ADJUST_MUTE else AudioManager.ADJUST_UNMUTE, 0) }
        }
    }

    private fun restoreAudio() {
        muteBeeps(false)
        val audio = getSystemService(AudioManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) audio.clearCommunicationDevice()
        else @Suppress("DEPRECATION") { audio.isSpeakerphoneOn = false }
        audio.mode = AudioManager.MODE_NORMAL
        proximity?.let { if (it.isHeld) it.release() }
        proximity = null
    }

    private fun hangUp() {
        val id = state.value.id
        if (id != null) scope.launch { runCatching { Session.client(applicationContext)?.call("call_end", JSONObject().put("id", id)) } }
        end(null)
    }

    /** The call is over (hung up here or there, or lost). */
    private fun end(error: String?) {
        _state.update { it.copy(phase = "ended", error = error ?: it.error) }
        scope.launch(Dispatchers.Main) {
            stopListening()
            recognizer?.destroy()
            recognizer = null
            SpeechPlayer.callAudio = false
            restoreAudio()
            Alerts.reschedule(applicationContext)  // the live connection back to what you chose
            stopSelf()
        }
    }

    private fun notification() = NotificationCompat.Builder(this, Ringer.CHANNEL_ONGOING)
        .setSmallIcon(R.drawable.ic_stat_lago)
        .setContentTitle("On a call with ${state.value.persona}")
        .setContentText(if (state.value.muted) "Muted" else "Tap to return to the call")
        .setOngoing(true)
        .setSilent(true)
        .setCategory(NotificationCompat.CATEGORY_CALL)
        .setContentIntent(PendingIntent.getActivity(this, 1, Intent(this, CallActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
        .addAction(0, if (state.value.muted) "Unmute" else "Mute", action(ACTION_MUTE, 2))
        .addAction(0, "Hang up", action(ACTION_HANG_UP, 3))
        .build()

    private fun updateNotification() {
        runCatching { androidx.core.app.NotificationManagerCompat.from(this).notify(NOTIFICATION_ID, notification()) }
    }

    private fun action(name: String, code: Int) = PendingIntent.getService(this, code,
        Intent(this, CallService::class.java).setAction(name), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    override fun onDestroy() {
        if (running === this) running = null
        recognizer?.destroy()
        recognizer = null
        if (state.value.phase != "ended") {
            SpeechPlayer.callAudio = false
            restoreAudio()
        }
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        const val EXTRA_ANSWER = "answer"
        private const val ACTION_HANG_UP = "hang_up"
        private const val ACTION_MUTE = "mute"
        private const val ACTION_SPEAKER = "speaker"
        private const val ACTION_INTERRUPT = "interrupt"
        private const val ACTION_SAY = "say"
        private const val EXTRA_TEXT = "text"
        private const val NOTIFICATION_ID = 7450  // 7444-7446 are the updater's
        private const val POLL_MS = 1500L
        private const val LOST_MS = 30_000L
        private val THINKING = setOf("working", "starting", "connecting")
        private val BEEP_STREAMS = intArrayOf(AudioManager.STREAM_MUSIC, AudioManager.STREAM_SYSTEM, AudioManager.STREAM_NOTIFICATION)
        private val HEADSETS = setOf(AudioDeviceInfo.TYPE_BLUETOOTH_SCO, AudioDeviceInfo.TYPE_WIRED_HEADSET,
            AudioDeviceInfo.TYPE_WIRED_HEADPHONES, AudioDeviceInfo.TYPE_USB_HEADSET, AudioDeviceInfo.TYPE_BLE_HEADSET)

        private val _state = MutableStateFlow(CallUi(phase = "ended"))
        val state: StateFlow<CallUi> = _state.asStateFlow()
        @Volatile private var running: CallService? = null
        val onCall: Boolean get() = running != null

        /** Call Lago, or answer his call (`answering` is its id). */
        fun start(context: Context, answering: String? = null) {
            if (running == null) _state.value = CallUi()
            val intent = Intent(context, CallService::class.java)
            if (answering != null) intent.putExtra(EXTRA_ANSWER, answering)
            ContextCompat.startForegroundService(context, intent)
        }

        fun send(context: Context, action: String) {
            context.startService(Intent(context, CallService::class.java).setAction(action))
        }

        fun hangUp(context: Context) = send(context, ACTION_HANG_UP)
        fun toggleMute(context: Context) = send(context, ACTION_MUTE)
        fun toggleSpeaker(context: Context) = send(context, ACTION_SPEAKER)
        fun interrupt(context: Context) = send(context, ACTION_INTERRUPT)

        /** Hand the call to Chip, Atlas or Lago: the same as saying so. */
        fun talkTo(context: Context, name: String) {
            if (running == null) return
            context.startService(Intent(context, CallService::class.java).setAction(ACTION_SAY)
                .putExtra(EXTRA_TEXT, "Let me talk to $name"))
        }
    }
}
