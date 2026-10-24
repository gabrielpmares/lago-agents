package com.lagoagents.mobile.net

import android.annotation.SuppressLint
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.X509TrustManager

/**
 * Trust exactly one certificate: your computer's, by its SHA-256 fingerprint.
 *
 * Your computer's certificate is self-signed, so no certificate authority can vouch for
 * it, and none should be able to: pinning the certificate itself means a certificate from
 * any authority, real or rogue, is refused. Because the one acceptable certificate is
 * fixed, the host name in it doesn't matter (the phone may reach the computer by its
 * Tailscale IP or its MagicDNS name), so [LagoClient] skips host name checks.
 */
@SuppressLint("CustomX509TrustManager")
class PinnedTrustManager(private val expected: String?) : X509TrustManager {
    /** The fingerprint of the certificate the last handshake presented. */
    @Volatile var seen: String? = null
        private set

    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        val leaf = chain?.firstOrNull() ?: throw CertificateException("No certificate")
        val fingerprint = sha256Hex(leaf.encoded)
        seen = fingerprint
        if (expected != null && !MessageDigest.isEqual(fingerprint.toByteArray(), expected.lowercase().toByteArray())) {
            throw CertificateException("This isn't the computer you paired with (its certificate changed)")
        }
    }

    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        throw CertificateException("Not a server")
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()

    fun socketFactory(): SSLSocketFactory =
        SSLContext.getInstance("TLS").apply { init(null, arrayOf(this@PinnedTrustManager), SecureRandom()) }.socketFactory

    companion object {
        fun sha256Hex(bytes: ByteArray): String =
            MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

        fun pretty(hex: String): String = hex.uppercase().chunked(2).joinToString(":")
    }
}
