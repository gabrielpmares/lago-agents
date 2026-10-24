package com.lagoagents.mobile.update

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import com.lagoagents.mobile.LagoApplication

/** Hears how an update install went (PackageInstaller's result for Updater.install). */
class InstallReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val name = intent.getStringExtra(EXTRA_NAME) ?: "the new version"
        val release = (Updater.state.value as? UpdateState.Installing)?.release
        when (val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                val confirm = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU)
                    intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                else @Suppress("DEPRECATION") intent.getParcelableExtra(Intent.EXTRA_INTENT)
                confirm ?: return
                if (LagoApplication.inForeground) {
                    context.startActivity(confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                } else {
                    Updater.notifyConfirm(context, confirm, name)
                }
            }
            PackageInstaller.STATUS_SUCCESS -> {}  // this process is replaced; the new version says so (BootReceiver)
            else -> {
                val detail = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)
                val message = when (status) {
                    PackageInstaller.STATUS_FAILURE_ABORTED -> "Update cancelled. Tap Update to try again."
                    PackageInstaller.STATUS_FAILURE_STORAGE -> "Not enough space on the phone for the update."
                    PackageInstaller.STATUS_FAILURE_CONFLICT, PackageInstaller.STATUS_FAILURE_INCOMPATIBLE ->
                        "Android refused the update: ${detail ?: "it doesn't fit this installation"}."
                    else -> "The update didn't install${detail?.let { ": $it" } ?: "."}"
                }
                Updater.failed(context, release, message)
            }
        }
    }

    companion object {
        const val EXTRA_CODE = "code"
        const val EXTRA_NAME = "name"
    }
}
