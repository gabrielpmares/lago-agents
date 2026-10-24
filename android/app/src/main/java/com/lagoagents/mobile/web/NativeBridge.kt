package com.lagoagents.mobile.web

import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.webkit.MimeTypeMap
import androidx.activity.ComponentActivity
import androidx.core.content.FileProvider
import com.lagoagents.mobile.net.LagoException
import com.lagoagents.mobile.ui.AppViewModel
import com.lagoagents.mobile.voice.Speech
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File

/**
 * Answers the page's calls (see assets/bridge.js). Most go to your computer unchanged;
 * the few that are about this phone are answered here:
 *
 * - show_terminal / hide_terminal / open_external: the native terminal mirror;
 * - mic_start / mic_stop / mic_cancel: the phone's speech recognition;
 * - open_attachment / open_url: open in the phone's own apps;
 * - clipboard_files: nothing (pasting pictures is a desktop shortcut).
 */
class NativeBridge(
    private val activity: ComponentActivity,
    private val vm: AppViewModel,
    private val askMic: suspend () -> Boolean,
    private val reply: (String) -> Unit,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val speech by lazy { Speech(activity) }
    @Volatile var dark: Boolean = true

    fun handle(message: String) {
        val first = message.indexOf('\n')
        val second = message.indexOf('\n', first + 1)
        if (first < 0 || second < 0) return
        val id = message.substring(0, first).toLongOrNull() ?: return
        val method = message.substring(first + 1, second)
        val params = message.substring(second + 1)
        if (method == "__settings") {
            activity.runOnUiThread { vm.openSettings(true) }
            return
        }
        scope.launch {
            val body = try {
                answer(method, params)
            } catch (error: LagoException.SignedOut) {
                withContext(Dispatchers.Main) { vm.signedOut() }
                fail("Signed out")
            } catch (error: LagoException.Unreachable) {
                vm.reportOffline(error.message)
                fail(error.message ?: "Can't reach your computer")
            } catch (error: LagoException) {
                fail(error.message ?: "That didn't work")
            } catch (error: Exception) {
                fail(error.message ?: error.javaClass.simpleName)
            }
            reply("R$id\n$body")
        }
    }

    private suspend fun answer(method: String, paramsJson: String): String {
        val params = { runCatching { JSONObject(paramsJson) }.getOrDefault(JSONObject()) }
        return when (method) {
            "show_terminal" -> {
                val body = forward(method, paramsJson)
                val shown = runCatching { JSONObject(body).getJSONObject("data").optBoolean("shown") }.getOrDefault(false)
                val agent = params().optString("agent_id")
                withContext(Dispatchers.Main) { if (shown) vm.openTerminal(agent) else vm.closeTerminal() }
                body
            }
            "hide_terminal" -> {
                withContext(Dispatchers.Main) { vm.closeTerminal() }
                ok(JSONObject().put("ok", true))
            }
            "open_external" -> {
                val agent = params().optString("agent_id")
                withContext(Dispatchers.Main) {
                    vm.openTerminal(agent)
                    if (vm.terminal.value?.expanded == false) vm.toggleTerminalExpanded()
                }
                ok(JSONObject().put("ok", true).put("message", "Opened full screen."))
            }
            "clipboard_files" -> ok(JSONObject().put("files", org.json.JSONArray()))
            "mic_start" -> {
                if (!askMic()) return fail("Allow the microphone for Lago Agents to talk to Lago")
                withContext(Dispatchers.Main) { speech.start() }
                ok(JSONObject().put("ok", true))
            }
            "mic_stop" -> {
                val text = withContext(Dispatchers.Main) { speech.stop() }
                val send = params().optBoolean("send", true)
                var sent = false
                if (text.isNotBlank() && send) {
                    client().call("say_to_orchestrator", JSONObject().put("text", text).put("spoken", true))
                    sent = true
                }
                ok(JSONObject().put("text", text).put("sent", sent))
            }
            "mic_cancel" -> {
                withContext(Dispatchers.Main) { speech.cancel() }
                ok(JSONObject().put("ok", true))
            }
            "open_attachment" -> {
                openAttachment(params().optString("path"))
                ok(JSONObject().put("ok", true))
            }
            "open_url" -> {
                val url = params().optString("url")
                if (!url.startsWith("https://") && !url.startsWith("http://")) return fail("Only web links can be opened")
                openOutside(Uri.parse(url), null)
                ok(JSONObject().put("ok", true))
            }
            "init" -> {
                // Follow the phone's light or dark setting from the first frame.
                val body = JSONObject(forward(method, paramsJson))
                body.optJSONObject("data")?.put("theme", if (dark) "dark" else "light")
                body.toString()
            }
            else -> forward(method, paramsJson)
        }
    }

    private fun client() = vm.client() ?: throw LagoException.SignedOut()

    private fun forward(method: String, paramsJson: String): String {
        val body = client().callRaw(method, paramsJson)
        vm.reportOffline(null)
        return body
    }

    /** Download a file you sent Lago and open it in the phone's app for it. */
    private suspend fun openAttachment(path: String) {
        val name = File(path).name
        if (name.isBlank() || name.contains('/')) throw LagoException.Refused("That file is no longer kept")
        val folder = File(activity.cacheDir, "attachments").apply { mkdirs() }
        folder.listFiles()?.filter { System.currentTimeMillis() - it.lastModified() > 86_400_000 }?.forEach { it.delete() }
        val target = File(folder, name)
        client().get("/v1/attachments/" + Uri.encode(name)).use { response ->
            if (response.code != 200) throw LagoException.Refused("That file is no longer kept")
            response.body?.byteStream()?.use { input -> target.outputStream().use { input.copyTo(it) } }
        }
        val uri = FileProvider.getUriForFile(activity, activity.packageName + ".files", target)
        val mime = MimeTypeMap.getSingleton().getMimeTypeFromExtension(target.extension.lowercase()) ?: "*/*"
        openOutside(uri, mime)
    }

    private suspend fun openOutside(uri: Uri, mime: String?) = withContext(Dispatchers.Main) {
        val intent = Intent(Intent.ACTION_VIEW).apply {
            if (mime != null) setDataAndType(uri, mime) else data = uri
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        vm.expectExternal()
        try {
            activity.startActivity(intent)
        } catch (_: ActivityNotFoundException) {
            throw LagoException.Refused("No app on this phone opens that")
        }
    }

    fun close() {
        runCatching { speech.cancel() }
        scope.cancel()
    }

    private companion object {
        fun ok(data: JSONObject) = JSONObject().put("ok", true).put("data", data).toString()
        fun fail(message: String) = JSONObject().put("ok", false).put("error", message).toString()
    }
}
