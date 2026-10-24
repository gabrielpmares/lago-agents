package com.lagoagents.mobile.net

import com.lagoagents.mobile.BuildConfig
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.IOException
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.CertificateException
import java.util.concurrent.TimeUnit
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import javax.net.ssl.SSLException
import javax.net.ssl.SSLHandshakeException

/** Why a request to your computer didn't work, in words worth showing. */
sealed class LagoException(message: String) : Exception(message) {
    /** No answer: the computer is off, asleep, or Tailscale is off on either side. */
    class Unreachable(message: String) : LagoException(message)
    /** The computer answered with a certificate other than the paired one. */
    class Untrusted(message: String) : LagoException(message)
    /** The session lapsed or was ended: sign in with the password again. */
    class SignedOut : LagoException("Sign in again")
    /** The computer no longer knows this phone (removed on the desktop): pair again. */
    class Unpaired(message: String) : LagoException(message)
    /** Wrong password or code, or too many attempts. */
    class Refused(message: String) : LagoException(message)
}

data class Credentials(val deviceId: String, val deviceKey: String, val token: String, val name: String)

/**
 * Talks to remote.py on your computer: HTTPS with the computer's certificate pinned,
 * a bearer token per session. Blocking calls; run them off the main thread.
 */
class LagoClient(val baseUrl: String, val fingerprint: String, @Volatile var token: String?) {
    private val http: OkHttpClient = build(PinnedTrustManager(fingerprint), readSeconds = 180)

    /** Calls an Api method. Returns the computer's JSON reply, `{"ok":…, "data"|"error":…}`. */
    fun callRaw(method: String, paramsJson: String): String {
        require(METHOD.matches(method)) { "Bad method" }
        val body = "{\"method\":\"$method\",\"params\":${paramsJson.ifBlank { "{}" }}}"
        return post("/v1/call", body)
    }

    fun call(method: String, params: JSONObject = JSONObject()): JSONObject {
        val reply = JSONObject(callRaw(method, params.toString()))
        if (!reply.optBoolean("ok")) throw LagoException.Refused(reply.optString("error", "That didn't work"))
        return reply.optJSONObject("data") ?: JSONObject()
    }

    /** A GET for the WebView (pages, styles, pictures). The caller closes the response. */
    fun get(path: String): Response {
        val request = Request.Builder().url(baseUrl + path).authorized().get().build()
        val response = execute(request)
        if (response.code == 401) {
            response.close()
            throw LagoException.SignedOut()
        }
        return response
    }

    /** Held open by the computer until what needs you changes (see remote.py AlertFeed). */
    private val events: OkHttpClient by lazy {
        http.newBuilder().readTimeout(80, TimeUnit.SECONDS).retryOnConnectionFailure(false).build()
    }

    /**
     * A call for the live alert feed: it answers when the list differs from [since], or
     * after about 50 seconds as a heartbeat. Cancel it to drop a connection that the
     * network change left hanging. Parse the answer with [readEvents].
     */
    fun eventsCall(since: Long): okhttp3.Call =
        events.newCall(Request.Builder().url("$baseUrl/v1/events?since=$since").authorized().get().build())

    fun readEvents(call: okhttp3.Call): JSONObject {
        val response = try {
            call.execute()
        } catch (error: IOException) {
            throw LagoException.Unreachable(error.message ?: "Connection lost")
        }
        response.use {
            val text = it.body?.string().orEmpty()
            if (it.code == 401) throw LagoException.SignedOut()
            if (it.code != 200) throw LagoException.Refused(errorOf(text, it.code))
            return JSONObject(text)
        }
    }

    fun logout() {
        runCatching { post("/v1/logout", "{}") }
    }

    private fun post(path: String, json: String): String {
        val request = Request.Builder().url(baseUrl + path).authorized()
            .post(json.toRequestBody(JSON)).build()
        execute(request).use { response ->
            val text = response.body?.string().orEmpty()
            if (response.code == 401) throw LagoException.SignedOut()
            if (response.code != 200) throw LagoException.Refused(errorOf(text, response.code))
            return text
        }
    }

    private fun Request.Builder.authorized() = apply { token?.let { header("Authorization", "Bearer $it") } }

    private fun execute(request: Request): Response = execute(http, request)

    companion object {
        private val JSON = "application/json; charset=utf-8".toMediaType()
        private val METHOD = Regex("^[a-z][a-z0-9_]{0,63}$")
        private const val PAIR_CLIENT = "lago-pair-client-v1|"
        private const val PAIR_SERVER = "lago-pair-server-v1|"

        private fun build(trust: PinnedTrustManager, readSeconds: Long) = OkHttpClient.Builder()
            // Tells the computer which version this phone runs (shown on its Phone page).
            .addInterceptor { chain ->
                // And where Lago should talk: on this phone (while the app is open, or always) or the computer.
                chain.proceed(chain.request().newBuilder()
                    .header("X-Lago-App", BuildConfig.VERSION_CODE.toString())
                    .header("X-Lago-Voice", com.lagoagents.mobile.voice.Presence.voiceMode)
                    .header("X-Lago-Foreground", if (com.lagoagents.mobile.voice.Presence.foreground) "1" else "0")
                    .build())
            }
            .sslSocketFactory(trust.socketFactory(), trust)
            .hostnameVerifier { _, _ -> true }  // the certificate itself is pinned; see PinnedTrustManager
            .connectTimeout(8, TimeUnit.SECONDS)
            .readTimeout(readSeconds, TimeUnit.SECONDS)
            .writeTimeout(120, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()

        private fun execute(client: OkHttpClient, request: Request): Response = try {
            client.newCall(request).execute()
        } catch (error: SSLHandshakeException) {
            if (error.cause is CertificateException) throw LagoException.Untrusted(error.cause?.message ?: "Untrusted certificate")
            throw LagoException.Unreachable("The secure connection failed: ${error.message}")
        } catch (error: SSLException) {
            throw LagoException.Unreachable("The secure connection failed: ${error.message}")
        } catch (error: IOException) {
            throw LagoException.Unreachable(
                "Can't reach your computer. Check that Tailscale is on here and there, and that the computer is awake."
            )
        }

        private fun errorOf(text: String, code: Int): String =
            runCatching { JSONObject(text).optString("error") }.getOrNull()?.takeIf { it.isNotBlank() }
                ?: "Your computer answered $code"

        private fun hmacHex(key: String, message: String): String {
            val mac = Mac.getInstance("HmacSHA256")
            mac.init(SecretKeySpec(key.toByteArray(Charsets.UTF_8), "HmacSHA256"))
            return mac.doFinal(message.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
        }

        /**
         * Pair this phone. Three steps:
         * 1. Connect and note the certificate's fingerprint (it must match the QR code's,
         *    when scanned).
         * 2. Prove we know the one-time code, bound to that fingerprint, and check the
         *    computer's proof back. Someone in the middle shows another certificate, so
         *    neither proof works for them, and the password is never sent to them.
         * 3. Send the password over the now-pinned connection; get this phone's key.
         */
        fun pair(url: String, expectedFingerprint: String?, code: String, password: String, deviceName: String): Pair<String, Credentials> {
            val probeTrust = PinnedTrustManager(expectedFingerprint)
            val probe = build(probeTrust, readSeconds = 15)
            execute(probe, Request.Builder().url("$url/v1/hello").get().build()).use { response ->
                val hello = runCatching { JSONObject(response.body?.string().orEmpty()) }.getOrNull()
                if (hello?.optString("app") != "lago-agents") {
                    throw LagoException.Refused("That address isn't Lago Agents' phone server")
                }
            }
            val fingerprint = probeTrust.seen ?: throw LagoException.Untrusted("No certificate seen")
            val normalCode = PairingLink.normalizeCode(code)
            if (normalCode.length != 10) throw LagoException.Refused("The code has 10 letters and digits")
            val nonce = ByteArray(16).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }
            val message = "$fingerprint|$nonce"
            val client = LagoClient(url, fingerprint, null)
            val start = JSONObject(client.post("/v1/pair/start", JSONObject()
                .put("nonce", nonce).put("proof", hmacHex(normalCode, PAIR_CLIENT + message)).toString()))
            val expected = hmacHex(normalCode, PAIR_SERVER + message)
            val serverProof = start.optString("server_proof")
            if (!MessageDigest.isEqual(expected.toByteArray(), serverProof.toByteArray())) {
                throw LagoException.Untrusted("The computer couldn't prove it's yours. Nothing was sent.")
            }
            val done = JSONObject(client.post("/v1/pair/finish", JSONObject()
                .put("ticket", start.getString("ticket")).put("password", password)
                .put("device_name", deviceName).toString()))
            return fingerprint to Credentials(
                done.getString("device_id"), done.getString("device_key"), done.getString("token"), done.optString("name", deviceName)
            )
        }

        /** A new session for a paired phone: its device key and the password. */
        fun login(url: String, fingerprint: String, deviceId: String, deviceKey: String, password: String): String {
            val client = LagoClient(url, fingerprint, null)
            val request = Request.Builder().url("$url/v1/login").post(
                JSONObject().put("device_id", deviceId).put("device_key", deviceKey).put("password", password)
                    .toString().toRequestBody(JSON)
            ).build()
            client.execute(request).use { response ->
                val text = response.body?.string().orEmpty()
                when (response.code) {
                    200 -> return JSONObject(text).getString("token")
                    410 -> throw LagoException.Unpaired(errorOf(text, 410))
                    else -> throw LagoException.Refused(errorOf(text, response.code))
                }
            }
        }

        fun isValidUrl(url: String) = runCatching { url.toHttpUrl() }.isSuccess
    }
}
