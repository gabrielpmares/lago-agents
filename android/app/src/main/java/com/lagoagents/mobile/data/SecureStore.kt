package com.lagoagents.mobile.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * The paired computer and this phone's credentials for it.
 *
 * [deviceKey] proves this phone was paired; [token] is the current session, which lapses
 * on the computer after a month unused (then the password is asked again).
 */
data class Profile(
    val serverUrl: String,
    val fingerprint: String,
    val deviceId: String,
    val deviceKey: String,
    val deviceName: String,
    val token: String?,
) {
    fun toJson(): String = JSONObject()
        .put("serverUrl", serverUrl).put("fingerprint", fingerprint)
        .put("deviceId", deviceId).put("deviceKey", deviceKey)
        .put("deviceName", deviceName).put("token", token ?: JSONObject.NULL)
        .toString()

    companion object {
        fun fromJson(text: String): Profile {
            val o = JSONObject(text)
            return Profile(
                serverUrl = o.getString("serverUrl"),
                fingerprint = o.getString("fingerprint"),
                deviceId = o.getString("deviceId"),
                deviceKey = o.getString("deviceKey"),
                deviceName = o.optString("deviceName", "Phone"),
                token = if (o.isNull("token")) null else o.getString("token"),
            )
        }
    }
}

/**
 * Secrets encrypted with an AES-256-GCM key that never leaves the Android Keystore
 * (hardware-backed where the phone has it). The ciphertext lives in private
 * SharedPreferences, excluded from backups.
 */
class SecureStore(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("lago_secure", Context.MODE_PRIVATE)

    @Synchronized
    fun loadProfile(): Profile? {
        val text = decrypt(prefs.getString(PROFILE, null) ?: return null) ?: return null
        return runCatching { Profile.fromJson(text) }.getOrNull()
    }

    @Synchronized
    fun saveProfile(profile: Profile?) {
        prefs.edit().apply {
            if (profile == null) remove(PROFILE) else putString(PROFILE, encrypt(profile.toJson()))
        }.commit()
    }

    fun updateToken(token: String?) {
        synchronized(this) {
            loadProfile()?.let { saveProfile(it.copy(token = token)) }
        }
    }

    private fun key(): SecretKey {
        val store = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (store.getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .setRandomizedEncryptionRequired(true)
                .build()
        )
        return generator.generateKey()
    }

    private fun encrypt(plain: String): String {
        val cipher = Cipher.getInstance(TRANSFORM)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val sealed = cipher.iv + cipher.doFinal(plain.toByteArray(Charsets.UTF_8))
        return Base64.encodeToString(sealed, Base64.NO_WRAP)
    }

    /** Null when the data can't be opened (the keystore key was wiped, say): re-pair then. */
    private fun decrypt(encoded: String): String? = runCatching {
        val sealed = Base64.decode(encoded, Base64.NO_WRAP)
        val cipher = Cipher.getInstance(TRANSFORM)
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, sealed, 0, IV_BYTES))
        String(cipher.doFinal(sealed, IV_BYTES, sealed.size - IV_BYTES), Charsets.UTF_8)
    }.getOrNull()

    private companion object {
        const val KEYSTORE = "AndroidKeyStore"
        const val ALIAS = "lago_profile_v1"
        const val TRANSFORM = "AES/GCM/NoPadding"
        const val IV_BYTES = 12
        const val PROFILE = "profile"
    }
}
