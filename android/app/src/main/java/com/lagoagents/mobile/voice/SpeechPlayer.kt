package com.lagoagents.mobile.voice

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.MediaPlayer
import com.lagoagents.mobile.data.Session
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** Where you want Lago's voice (the phone tells the computer with every request). */
object Presence {
    /** "open": on the phone while the app is on screen; "always": also in the background; "off": the computer. */
    @Volatile var voiceMode: String = "open"
    @Volatile var foreground: Boolean = false
}

/**
 * Plays Lago's voice on the phone. The computer still makes the speech (same voice, effect
 * and chime as at the desk) but, while you're on the phone, writes it to a file instead of
 * its speakers; the live connection announces each one and this downloads and plays them in
 * order, ducking music while Lago talks. A hush on the computer stops it here too.
 */
object SpeechPlayer {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val queue = Channel<Pair<String, Long>>(Channel.UNLIMITED)  // id, when announced
    private val known = LinkedHashSet<String>()
    private var lastHush = -1L
    @Volatile private var player: MediaPlayer? = null
    @Volatile private var playing: CompletableDeferred<Unit>? = null
    @Volatile private var hushedAt = 0L
    private var started = false

    /** What the live connection says is waiting. */
    @Synchronized
    fun announced(context: Context, speech: JSONArray?, hush: Long) {
        if (lastHush >= 0 && hush != lastHush) stop()
        lastHush = hush
        speech ?: return
        start(context.applicationContext)
        for (i in 0 until speech.length()) {
            val id = speech.getJSONObject(i).optString("id")
            if (id.isNotEmpty() && known.add(id)) queue.trySend(id to System.currentTimeMillis())
        }
        while (known.size > 200) known.remove(known.first())
    }

    private fun start(context: Context) {
        if (started) return
        started = true
        scope.launch {
            for ((id, at) in queue) {
                runCatching { play(context, id, at) }
                runCatching { Session.client(context)?.call("speech_done", JSONObject().put("id", id)) }
            }
        }
    }

    private suspend fun play(context: Context, id: String, at: Long) {
        if (at < hushedAt) return
        val client = Session.client(context) ?: return
        val file = File(context.cacheDir, "speech-$id")
        client.get("/v1/speech/$id").use { response ->
            if (response.code != 200) return
            response.body?.byteStream()?.use { input -> file.outputStream().use { input.copyTo(it) } }
        }
        try {
            if (at < hushedAt) return
            playFile(context, file)
        } finally {
            file.delete()
        }
    }

    private suspend fun playFile(context: Context, file: File) {
        val audio = context.getSystemService(AudioManager::class.java)
        val attributes = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_ASSISTANT)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .build()
        val focus = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
            .setAudioAttributes(attributes).build()
        audio.requestAudioFocus(focus)
        val done = CompletableDeferred<Unit>().also { playing = it }
        try {
            withContext(Dispatchers.Main) {
                player = MediaPlayer().apply {
                    setAudioAttributes(attributes)
                    setDataSource(file.path)
                    setOnCompletionListener { done.complete(Unit) }
                    setOnErrorListener { _, _, _ -> done.complete(Unit); true }
                    prepare()
                    start()
                }
            }
            done.await()
        } finally {
            playing = null
            withContext(Dispatchers.Main) {
                player?.release()
                player = null
            }
            audio.abandonAudioFocusRequest(focus)
        }
    }

    /** Stop now and skip what's queued (Lago was hushed on the computer). */
    fun stop() {
        hushedAt = System.currentTimeMillis() + 1
        playing?.complete(Unit)
        scope.launch(Dispatchers.Main) {
            runCatching { player?.stop() }
            player?.let { runCatching { it.release() } }
            player = null
        }
    }
}
