package com.lagoagents.mobile.update

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.lagoagents.mobile.BuildConfig
import com.lagoagents.mobile.LagoApplication
import com.lagoagents.mobile.MainActivity
import com.lagoagents.mobile.R
import com.lagoagents.mobile.data.AppSettings
import com.lagoagents.mobile.data.Session
import com.lagoagents.mobile.net.LagoException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/** A version of the app published on your computer (remote.py publish_app). */
data class AppRelease(val code: Int, val name: String, val sha256: String, val size: Long, val notes: String) {
    companion object {
        fun from(json: JSONObject?): AppRelease? {
            json ?: return null
            val code = json.optInt("code", 0).takeIf { it > 0 } ?: return null
            return AppRelease(code, json.optString("name", "$code"), json.optString("sha256"),
                json.optLong("size"), json.optString("notes"))
        }
    }
}

sealed interface UpdateState {
    data object None : UpdateState
    data class Available(val release: AppRelease, val message: String? = null) : UpdateState
    data class Downloading(val release: AppRelease, val progress: Float) : UpdateState
    data class Installing(val release: AppRelease) : UpdateState
}

/**
 * Updates the app from your computer, over the same pinned Tailscale connection.
 *
 * The live alert feed announces each published version the moment it's published. A newer
 * one is downloaded, checked (its SHA-256 against what the computer announced, its package
 * name and version, and that it's signed with this app's own key), and installed over
 * this app with Android's PackageInstaller: pairing, settings and data stay.
 *
 * With "update automatically" on, that happens as soon as the app isn't on screen (or
 * right away when you tap Update). Android installs silently when it allows (Android 12+,
 * once this app installed the previous version itself); otherwise it asks you with one tap.
 */
object Updater {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val _state = MutableStateFlow<UpdateState>(UpdateState.None)
    val state: StateFlow<UpdateState> = _state.asStateFlow()

    const val CHANNEL = "updates"
    const val EXTRA_UPDATE = "update_now"
    private const val NOTIFICATION_ID = 7444
    private const val BACKGROUND_GRACE_MS = 30_000L
    private const val ALLOW_MESSAGE = "Allow Lago Agents to install updates (the switch on the next screen)."

    /** What the computer says is published (from the live feed or a check). */
    fun announced(context: Context, json: JSONObject?) {
        val release = AppRelease.from(json) ?: return
        if (release.code <= BuildConfig.VERSION_CODE) {
            if (_state.value !is UpdateState.None) _state.value = UpdateState.None
            return
        }
        when (val current = _state.value) {
            is UpdateState.Downloading, is UpdateState.Installing -> if (releaseOf(current)?.code == release.code) return
            is UpdateState.Available -> if (current.release == release && current.message != null) return
            UpdateState.None -> {}
        }
        _state.value = UpdateState.Available(release)
        val auto = AppSettings.get(context).value.autoUpdate
        when {
            LagoApplication.inForeground -> {}            // the banner in the app shows it
            auto -> onBackground(context)                 // install while you're elsewhere
            else -> notifyAvailable(context, release)
        }
    }

    /** Ask the computer what's published (on start and from the 15-minute check). */
    fun check(context: Context) {
        val client = Session.client(context) ?: return
        val reply = client.get("/v1/app/latest").use { JSONObject(it.body?.string().orEmpty()) }
        announced(context, reply.optJSONObject("app"))
    }

    /** Back in the app: if you just allowed installing updates, carry on without another tap. */
    fun onForeground(context: Context) {
        val waiting = _state.value as? UpdateState.Available ?: return
        if (waiting.message == ALLOW_MESSAGE && context.packageManager.canRequestPackageInstalls()) start(context, foreground = true)
    }

    /** The app went to the background: a waiting update installs now, if automatic. */
    fun onBackground(context: Context) {
        if (_state.value !is UpdateState.Available || !AppSettings.get(context).value.autoUpdate) return
        scope.launch {
            delay(BACKGROUND_GRACE_MS)  // not for a quick trip to the file picker or a permission
            if (!LagoApplication.inForeground && _state.value is UpdateState.Available) start(context, foreground = false)
        }
    }

    /** Download, check and install the announced version. */
    fun start(context: Context, foreground: Boolean) {
        val release = (_state.value as? UpdateState.Available)?.release ?: return
        val app = context.applicationContext
        if (!app.packageManager.canRequestPackageInstalls()) {
            if (foreground) {
                _state.value = UpdateState.Available(release, ALLOW_MESSAGE)
                app.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + app.packageName))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            } else {
                notifyAvailable(app, release)
            }
            return
        }
        _state.value = UpdateState.Downloading(release, 0f)
        scope.launch {
            try {
                val apk = download(app, release)
                verify(app, apk, release)
                _state.value = UpdateState.Installing(release)
                install(app, apk, release)
            } catch (error: Exception) {
                val message = when (error) {
                    is LagoException.Unreachable -> "Couldn't download it: your computer isn't reachable."
                    else -> error.message ?: "The update didn't work."
                }
                failed(app, release, message)
            }
        }
    }

    fun failed(context: Context, release: AppRelease?, message: String) {
        release ?: return
        _state.value = UpdateState.Available(release, message)
        if (!LagoApplication.inForeground) notifyAvailable(context, release, message)
    }

    private fun releaseOf(state: UpdateState) = when (state) {
        is UpdateState.Available -> state.release
        is UpdateState.Downloading -> state.release
        is UpdateState.Installing -> state.release
        UpdateState.None -> null
    }

    private fun download(context: Context, release: AppRelease): File {
        val client = Session.client(context) ?: throw LagoException.SignedOut()
        val folder = File(context.cacheDir, "update").apply { mkdirs() }
        folder.listFiles()?.forEach { it.delete() }
        val target = File(folder, "LagoAgents-${release.code}.apk")
        client.get("/v1/app/apk").use { response ->
            if (response.code != 200) throw IllegalStateException("Your computer has no update to give (${response.code}).")
            val body = response.body ?: throw IllegalStateException("Empty download")
            val total = body.contentLength().takeIf { it > 0 } ?: release.size
            body.byteStream().use { input ->
                target.outputStream().use { output ->
                    val buffer = ByteArray(64 * 1024)
                    var done = 0L
                    while (true) {
                        val n = input.read(buffer)
                        if (n < 0) break
                        output.write(buffer, 0, n)
                        done += n
                        if (total > 0) _state.value = UpdateState.Downloading(release, (done.toFloat() / total).coerceIn(0f, 1f))
                    }
                }
            }
        }
        return target
    }

    /** Refuse anything but this app, at the announced version, signed with this app's key. */
    private fun verify(context: Context, apk: File, release: AppRelease) {
        val digest = MessageDigest.getInstance("SHA-256")
        apk.inputStream().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val n = input.read(buffer)
                if (n < 0) break
                digest.update(buffer, 0, n)
            }
        }
        val sha = digest.digest().joinToString("") { "%02x".format(it) }
        if (!MessageDigest.isEqual(sha.toByteArray(), release.sha256.lowercase().toByteArray())) {
            apk.delete()
            throw SecurityException("The download was damaged (its checksum doesn't match). Try again.")
        }
        val pm = context.packageManager
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) PackageManager.GET_SIGNING_CERTIFICATES
        else @Suppress("DEPRECATION") PackageManager.GET_SIGNATURES
        val archive = pm.getPackageArchiveInfo(apk.path, flags) ?: throw SecurityException("That file isn't an app.")
        if (archive.packageName != context.packageName) {
            throw SecurityException("That update is for ${archive.packageName}, not this app (${context.packageName}).")
        }
        if (versionCode(archive) != release.code.toLong()) throw SecurityException("The update's version doesn't match what was announced.")
        val installed = pm.getPackageInfo(context.packageName, flags)
        if (signatures(archive) != signatures(installed)) {
            apk.delete()
            throw SecurityException("That update is signed with a different key, so Android can't install it over this app. " +
                "Build it with the same key (~/.local/share/lago-agents/android/).")
        }
    }

    @Suppress("DEPRECATION")
    private fun versionCode(info: PackageInfo): Long =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) info.longVersionCode else info.versionCode.toLong()

    @Suppress("DEPRECATION")
    private fun signatures(info: PackageInfo): Set<String> {
        val sigs = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            info.signingInfo?.let { if (it.hasMultipleSigners()) it.apkContentsSigners else it.signingCertificateHistory }
        } else info.signatures
        return sigs.orEmpty().map { sig ->
            MessageDigest.getInstance("SHA-256").digest(sig.toByteArray()).joinToString("") { "%02x".format(it) }
        }.toSet()
    }

    private fun install(context: Context, apk: File, release: AppRelease) {
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(context.packageName)
            setSize(apk.length())
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) setRequestUpdateOwnership(true)
        }
        val id = installer.createSession(params)
        installer.openSession(id).use { session ->
            session.openWrite("base.apk", 0, apk.length()).use { out ->
                apk.inputStream().use { it.copyTo(out) }
                session.fsync(out)
            }
            val intent = Intent(context, InstallReceiver::class.java)
                .putExtra(InstallReceiver.EXTRA_CODE, release.code)
                .putExtra(InstallReceiver.EXTRA_NAME, release.name)
            // Mutable: the system fills in the result.
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or
                (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0)
            session.commit(PendingIntent.getBroadcast(context, id, intent, flags).intentSender)
        }
    }

    fun notifyAvailable(context: Context, release: AppRelease, problem: String? = null) {
        val open = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .putExtra(EXTRA_UPDATE, true)
        val tap = PendingIntent.getActivity(context, NOTIFICATION_ID, open, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val text = problem ?: release.notes.lineSequence().firstOrNull { it.isNotBlank() }?.removePrefix("- ") ?: "Tap to update."
        notify(context, NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_lago)
            .setColor(0xFF4FB89E.toInt())
            .setContentTitle("Lago Agents ${release.name} is ready")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(problem ?: release.notes.ifBlank { "Tap to update." }))
            .setContentIntent(tap)
            .addAction(0, "Update", tap)
            .setAutoCancel(true))
    }

    /** The system needs your OK (first self-update, or older Android): one tap. */
    fun notifyConfirm(context: Context, confirm: Intent, name: String) {
        val tap = PendingIntent.getActivity(context, NOTIFICATION_ID + 1, confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        notify(context, NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_lago)
            .setColor(0xFF4FB89E.toInt())
            .setContentTitle("Finish updating Lago Agents to $name")
            .setContentText("Tap, then Update. Android asks this once in a while.")
            .setContentIntent(tap)
            .setAutoCancel(true))
    }

    fun notifyUpdated(context: Context) {
        val open = PendingIntent.getActivity(context, NOTIFICATION_ID + 2,
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        notify(context, NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_lago)
            .setColor(0xFF4FB89E.toInt())
            .setContentTitle("Lago Agents updated to ${BuildConfig.VERSION_NAME}")
            .setContentText("Tap to open it.")
            .setContentIntent(open)
            .setAutoCancel(true)
            .setTimeoutAfter(6 * 3600_000L))
    }

    private fun notify(context: Context, builder: NotificationCompat.Builder) {
        runCatching { NotificationManagerCompat.from(context).notify(NOTIFICATION_ID, builder.build()) }
    }
}
