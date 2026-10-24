package com.lagoagents.mobile.net

import android.net.Uri

/**
 * What the desktop's pairing QR code holds:
 * `lago://pair?u=https://100.x.y.z:7443&f=<certificate sha-256>&c=ABCDE-FGHJK`.
 */
data class PairingLink(val url: String, val fingerprint: String?, val code: String) {
    companion object {
        const val DEFAULT_PORT = 7443

        fun parse(text: String): PairingLink? {
            val uri = runCatching { Uri.parse(text.trim()) }.getOrNull() ?: return null
            if (uri.scheme != "lago" || uri.host != "pair") return null
            val url = normalizeUrl(uri.getQueryParameter("u") ?: return null) ?: return null
            val fp = uri.getQueryParameter("f")?.lowercase()?.takeIf { it.matches(Regex("^[0-9a-f]{64}$")) }
            val code = uri.getQueryParameter("c")?.takeIf { normalizeCode(it).length == 10 } ?: return null
            return PairingLink(url, fp, code)
        }

        /** "my-laptop", "100.1.2.3" or "https://host:port" → "https://host:port". */
        fun normalizeUrl(input: String): String? {
            var text = input.trim().trimEnd('/')
            if (text.isEmpty()) return null
            if (text.startsWith("http://", ignoreCase = true)) return null  // never plain HTTP
            if (!text.startsWith("https://", ignoreCase = true)) text = "https://$text"
            val uri = runCatching { Uri.parse(text) }.getOrNull() ?: return null
            val host = uri.host?.takeIf { it.isNotBlank() } ?: return null
            val port = if (uri.port > 0) uri.port else DEFAULT_PORT
            val shownHost = if (host.contains(':')) "[$host]" else host
            return "https://$shownHost:$port"
        }

        fun normalizeCode(code: String) = code.uppercase().filter { it in 'A'..'Z' || it in '0'..'9' }
    }
}
