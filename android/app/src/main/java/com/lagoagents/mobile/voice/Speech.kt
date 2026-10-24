package com.lagoagents.mobile.voice

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Talk to Lago from the phone: the page's microphone button records here, with Android's
 * speech recognition, instead of on the desktop's microphone. Recognition stops by itself
 * after a pause, so it restarts until you press the button again, keeping every phrase.
 * Call everything on the main thread.
 */
class Speech(private val context: Context) {
    private var recognizer: SpeechRecognizer? = null
    private val heard = StringBuilder()
    private var partial = ""
    private var listening = false
    private var stopping: CompletableDeferred<Unit>? = null

    val available: Boolean get() = SpeechRecognizer.isRecognitionAvailable(context)

    fun start() {
        if (!available) throw IllegalStateException("This phone has no speech recognition. Type instead.")
        cancel()
        heard.setLength(0)
        partial = ""
        listening = true
        recognizer = SpeechRecognizer.createSpeechRecognizer(context).apply { setRecognitionListener(listener) }
        listen()
    }

    /** Stop and return all that was heard (empty if nothing). */
    suspend fun stop(): String {
        val r = recognizer ?: return heard.toString().trim()
        listening = false
        val done = CompletableDeferred<Unit>().also { stopping = it }
        r.stopListening()
        withTimeoutOrNull(4000) { done.await() }
        if (partial.isNotBlank()) append(partial)  // a phrase the recognizer hadn't finished
        destroy()
        return heard.toString().trim()
    }

    fun cancel() {
        listening = false
        destroy()
    }

    private fun destroy() {
        recognizer?.destroy()
        recognizer = null
        stopping?.complete(Unit)
        stopping = null
    }

    private fun listen() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 2500L)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 2000L)
        }
        recognizer?.startListening(intent)
    }

    private fun append(text: String) {
        if (text.isBlank()) return
        if (heard.isNotEmpty()) heard.append(' ')
        heard.append(text.trim())
        partial = ""
    }

    private val listener = object : RecognitionListener {
        override fun onResults(results: Bundle?) {
            append(results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty())
            if (listening) listen() else stopping?.complete(Unit)
        }

        override fun onPartialResults(partialResults: Bundle?) {
            partial = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty()
        }

        override fun onError(error: Int) {
            // A pause with nothing said, or the recognizer timing out: keep listening.
            if (listening && (error == SpeechRecognizer.ERROR_NO_MATCH || error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT)) {
                listen()
                return
            }
            listening = false
            stopping?.complete(Unit)
        }

        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onEvent(eventType: Int, params: Bundle?) {}
    }
}
