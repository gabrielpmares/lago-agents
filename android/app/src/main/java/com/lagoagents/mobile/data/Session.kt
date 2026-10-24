package com.lagoagents.mobile.data

import android.content.Context
import com.lagoagents.mobile.net.LagoClient

/**
 * The paired computer's profile and the one client for it, shared by the screens, the
 * WebView and the background alert checks. Kept in memory once decrypted.
 */
object Session {
    private val lock = Any()
    @Volatile private var loaded = false
    @Volatile private var profile: Profile? = null
    @Volatile private var client: LagoClient? = null

    fun profile(context: Context): Profile? = synchronized(lock) {
        if (!loaded) {
            profile = SecureStore(context).loadProfile()
            loaded = true
        }
        profile
    }

    /** A client with the current session token, or null when signed out or unpaired. */
    fun client(context: Context): LagoClient? = synchronized(lock) {
        val p = profile(context) ?: return null
        if (p.token == null) return null
        client ?: LagoClient(p.serverUrl, p.fingerprint, p.token).also { client = it }
    }

    fun save(context: Context, next: Profile?) = synchronized(lock) {
        SecureStore(context).saveProfile(next)
        profile = next
        loaded = true
        client = null
    }

    fun dropToken(context: Context) = synchronized(lock) {
        profile(context)?.let { save(context, it.copy(token = null)) }
    }
}
