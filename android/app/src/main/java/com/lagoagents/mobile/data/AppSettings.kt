package com.lagoagents.mobile.data

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** Preferences that aren't secret. One instance per process (see [AppSettings.get]). */
data class Prefs(
    val lockEnabled: Boolean = true,
    /** How long the app may sit in the background before it locks again. */
    val lockAfterMs: Long = 60_000,
    /** Blocks screenshots and blanks the app in the recent-apps list. */
    val secureScreen: Boolean = true,
    /** A check every 15 minutes for what needs you, while the app is closed. */
    val backgroundAlerts: Boolean = true,
    /** A live connection the computer pushes through (a quiet permanent notification). */
    val instantAlerts: Boolean = true,
    /** Narrow a mirrored terminal to the phone's width while you look at it. */
    val terminalFit: Boolean = true,
    val terminalWrap: Boolean = true,
    val terminalFontSp: Float = 11f,
    /** Install a new version from your computer as soon as the app isn't on screen. */
    val autoUpdate: Boolean = true,
    /** Where Lago talks: "open" (this phone while the app is on screen), "always", or "off" (the computer). */
    val voiceMode: String = "open",
)

class AppSettings private constructor(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("lago_prefs", Context.MODE_PRIVATE)
    private val state = MutableStateFlow(read())
    val flow: StateFlow<Prefs> = state.asStateFlow()
    val value: Prefs get() = state.value

    private fun read() = Prefs(
        lockEnabled = prefs.getBoolean("lockEnabled", true),
        lockAfterMs = prefs.getLong("lockAfterMs", 60_000),
        secureScreen = prefs.getBoolean("secureScreen", true),
        backgroundAlerts = prefs.getBoolean("backgroundAlerts", true),
        instantAlerts = prefs.getBoolean("instantAlerts", true),
        terminalFit = prefs.getBoolean("terminalFit", true),
        terminalWrap = prefs.getBoolean("terminalWrap", true),
        terminalFontSp = prefs.getFloat("terminalFontSp", 11f),
        autoUpdate = prefs.getBoolean("autoUpdate", true),
        voiceMode = prefs.getString("voiceMode", "open") ?: "open",
    )

    fun update(change: (Prefs) -> Prefs) {
        val next = change(state.value)
        prefs.edit()
            .putBoolean("lockEnabled", next.lockEnabled)
            .putLong("lockAfterMs", next.lockAfterMs)
            .putBoolean("secureScreen", next.secureScreen)
            .putBoolean("backgroundAlerts", next.backgroundAlerts)
            .putBoolean("instantAlerts", next.instantAlerts)
            .putBoolean("terminalFit", next.terminalFit)
            .putBoolean("terminalWrap", next.terminalWrap)
            .putFloat("terminalFontSp", next.terminalFontSp)
            .putBoolean("autoUpdate", next.autoUpdate)
            .putString("voiceMode", next.voiceMode)
            .apply()
        state.value = next
        com.lagoagents.mobile.voice.Presence.voiceMode = next.voiceMode
    }

    /** Alert keys already shown, so each one notifies once. */
    fun seenAlerts(): Set<String> = prefs.getStringSet("seenAlerts", emptySet()) ?: emptySet()

    fun setSeenAlerts(keys: Collection<String>) {
        prefs.edit().putStringSet("seenAlerts", keys.toSet()).apply()
    }

    /** Keys of the notifications on screen now, withdrawn once dealt with. */
    fun shownAlerts(): Set<String> = prefs.getStringSet("shownAlerts", emptySet()) ?: emptySet()

    fun setShownAlerts(keys: Collection<String>) {
        prefs.edit().putStringSet("shownAlerts", keys.toSet()).apply()
    }

    fun clearAlerts() = prefs.edit().remove("seenAlerts").remove("shownAlerts").apply()

    companion object {
        @Volatile private var instance: AppSettings? = null
        fun get(context: Context): AppSettings =
            instance ?: synchronized(this) { instance ?: AppSettings(context).also { instance = it } }
    }
}
