package com.lagoagents.mobile.alerts

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Live alerts come back on by themselves after the phone restarts or the app updates;
 * after an update, a note says which version you now have. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED || intent.action == Intent.ACTION_MY_PACKAGE_REPLACED) {
            Alerts.reschedule(context)
            if (intent.action == Intent.ACTION_MY_PACKAGE_REPLACED) com.lagoagents.mobile.update.Updater.notifyUpdated(context)
        }
    }
}
