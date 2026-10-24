package com.lagoagents.mobile.web

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.util.Log
import android.webkit.ConsoleMessage
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.webkit.JavaScriptReplyProxy
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import com.lagoagents.mobile.BuildConfig
import com.lagoagents.mobile.net.LagoException
import com.lagoagents.mobile.ui.AppViewModel
import org.json.JSONObject
import java.io.ByteArrayInputStream

/**
 * The full Lago Agents interface, the same pages the desktop window shows.
 *
 * The WebView never touches the network itself. Its pages live at a made-up origin
 * ([ORIGIN], the host Android reserves for this) and every request to it is answered
 * here: page files come from your computer through the pinned [com.lagoagents.mobile.net.LagoClient],
 * the bridge script from the app. Requests anywhere else are refused, links to the web
 * open in the phone's browser, and the native bridge is offered to this origin only.
 */
class LagoWebView(
    private val activity: ComponentActivity,
    private val vm: AppViewModel,
    private val pickFiles: (Array<String>, (Array<Uri>?) -> Unit) -> Unit,
    askMic: suspend () -> Boolean,
) {
    val view: WebView = WebView(activity)
    private val bridge = NativeBridge(activity, vm, askMic) { reply -> post(reply) }
    @Volatile private var proxy: JavaScriptReplyProxy? = null
    @Volatile private var ready = false

    init {
        configure()
        vm.onTerminalEnded = { id -> post(JSONObject().put("type", "terminalEnded").put("id", id).toString()) }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configure() {
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
        view.setBackgroundColor(Color.TRANSPARENT)
        view.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            setSupportMultipleWindows(false)
            javaScriptCanOpenWindowsAutomatically = false
            setGeolocationEnabled(false)
            mediaPlaybackRequiresUserGesture = true
            builtInZoomControls = false
            textZoom = 100
            cacheMode = WebSettings.LOAD_NO_CACHE
            userAgentString = "$userAgentString LagoAgentsAndroid/${BuildConfig.VERSION_NAME}"
        }
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            throw IllegalStateException("Update Android System WebView from the Play Store to use Lago Agents.")
        }
        WebViewCompat.addWebMessageListener(view, "LagoNative", setOf(ORIGIN)) { _, message, origin, isMainFrame, replyProxy ->
            if (!isMainFrame || origin.toString().trimEnd('/') != ORIGIN) return@addWebMessageListener
            proxy = replyProxy
            message.data?.let { bridge.handle(it) }
        }
        view.webViewClient = Client()
        view.webChromeClient = Chrome()
    }

    fun load(hash: String? = null) {
        ready = false
        proxy = null
        view.loadUrl("$ORIGIN/ui/index.html" + (hash ?: ""))
    }

    fun reload() = view.reload()

    /** Send the page a message (a reply or an event). Main thread. */
    fun post(message: String) {
        activity.runOnUiThread { runCatching { proxy?.postMessage(message) } }
    }

    fun setTheme(dark: Boolean) {
        bridge.dark = dark
        post(JSONObject().put("type", "theme").put("theme", if (dark) "dark" else "light").toString())
    }

    /** Open a page (a "#…" link from a notification), loading the app first if needed. */
    fun navigate(hash: String) {
        if (!hash.startsWith("#")) return
        if (ready && proxy != null) post(JSONObject().put("type", "navigate").put("hash", hash).toString())
        else load(hash)
    }

    fun destroy() {
        bridge.close()
        view.destroy()
    }

    private inner class Client : WebViewClient() {
        override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse {
            val url = request.url
            if (url.scheme != "https" || url.host != HOST) return refuse(403)
            val path = url.encodedPath ?: "/"
            return when {
                path == "/native/bridge.js" -> asset("bridge.js", "application/javascript")
                path.startsWith("/ui/") -> proxy("/v1" + path + (url.encodedQuery?.let { "?$it" } ?: ""), request.isForMainFrame)
                path.startsWith("/v1/attachments/") -> proxy(path, false)
                else -> refuse(404)
            }
        }

        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val url = request.url
            if (url.host == HOST) return false
            if (url.scheme == "http" || url.scheme == "https" || url.scheme == "mailto") {
                vm.expectExternal()
                try {
                    activity.startActivity(Intent(Intent.ACTION_VIEW, url).addCategory(Intent.CATEGORY_BROWSABLE))
                } catch (_: ActivityNotFoundException) {}
            }
            return true
        }

        override fun onPageFinished(view: WebView, url: String?) {
            ready = url?.startsWith(ORIGIN) == true
        }

        override fun onRenderProcessGone(view: WebView, detail: android.webkit.RenderProcessGoneDetail): Boolean {
            // The page's process died (memory pressure): start the page again.
            activity.runOnUiThread { activity.recreate() }
            return true
        }
    }

    private fun proxy(path: String, mainFrame: Boolean): WebResourceResponse {
        val client = vm.client() ?: return refuse(401)
        return try {
            val response = client.get(path)
            vm.reportOffline(null)
            val type = response.header("Content-Type") ?: "application/octet-stream"
            val mime = type.substringBefore(';').trim()
            val charset = Regex("charset=([\\w-]+)").find(type)?.groupValues?.get(1)
            val body = response.body?.byteStream() ?: ByteArrayInputStream(ByteArray(0))
            val headers = mutableMapOf("Cache-Control" to "no-store")
            response.header("Content-Security-Policy")?.let { headers["Content-Security-Policy"] = it }
            WebResourceResponse(mime, charset, response.code, response.message.ifBlank { "OK" }, headers, body)
        } catch (error: LagoException.SignedOut) {
            activity.runOnUiThread { vm.signedOut() }
            refuse(401)
        } catch (error: LagoException) {
            vm.reportOffline(error.message)
            if (mainFrame) offlinePage(error.message ?: "") else refuse(502)
        }
    }

    private fun asset(name: String, mime: String) =
        WebResourceResponse(mime, "utf-8", activity.assets.open(name))

    private fun refuse(code: Int) = WebResourceResponse(
        "text/plain", "utf-8", code, REASONS[code] ?: "Refused", mapOf("Cache-Control" to "no-store"),
        ByteArrayInputStream(ByteArray(0)),
    )

    /** What shows instead of the app when the computer can't be reached at start. */
    private fun offlinePage(message: String): WebResourceResponse {
        val safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        val html = """
            <!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
            <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
            <style>
              :root { color-scheme: dark light; }
              body { margin: 0; min-height: 100vh; display: grid; place-items: center; font: 15px/1.5 system-ui, sans-serif;
                     background: Canvas; color: CanvasText; }
              main { max-width: 360px; padding: 24px; text-align: center; }
              h1 { font-size: 20px; margin: 0 0 8px; }
              p { opacity: .75; margin: 0 0 6px; }
            </style></head><body><main>
            <h1>Can't reach your computer</h1><p>$safe</p>
            <p>Tap Try Again at the top once it's back.</p>
            </main></body></html>
        """.trimIndent()
        return WebResourceResponse("text/html", "utf-8", 200, "OK", mapOf("Cache-Control" to "no-store"),
            ByteArrayInputStream(html.toByteArray()))
    }

    private inner class Chrome : WebChromeClient() {
        override fun onShowFileChooser(
            webView: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams,
        ): Boolean {
            val types = params.acceptTypes.filter { it.isNotBlank() }.flatMap { it.split(',') }
                .map { it.trim() }.map { if (it.startsWith(".")) mimeFor(it) else it }
                .ifEmpty { listOf("*/*") }.toTypedArray()
            vm.expectExternal()
            pickFiles(types) { uris -> callback.onReceiveValue(uris) }
            return true
        }

        override fun onConsoleMessage(message: ConsoleMessage): Boolean {
            if (BuildConfig.DEBUG) Log.d("LagoPage", "${message.message()} (${message.sourceId()}:${message.lineNumber()})")
            return true
        }
    }

    companion object {
        const val HOST = "appassets.androidplatform.net"
        const val ORIGIN = "https://$HOST"
        private val REASONS = mapOf(401 to "Unauthorized", 403 to "Forbidden", 404 to "Not Found", 502 to "Bad Gateway")

        private fun mimeFor(extension: String) = when (extension.lowercase()) {
            ".png" -> "image/png"; ".jpg", ".jpeg" -> "image/jpeg"; ".gif" -> "image/gif"; ".webp" -> "image/webp"
            ".pdf" -> "application/pdf"; ".txt", ".md", ".csv", ".log" -> "text/*"; ".json" -> "application/json"
            else -> "*/*"
        }
    }
}
