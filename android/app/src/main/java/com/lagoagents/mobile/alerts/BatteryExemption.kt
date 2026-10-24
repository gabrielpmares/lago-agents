package com.lagoagents.mobile.alerts

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import android.provider.Settings

/**
 * Battery optimization can suspend the live connection while the phone sleeps (some
 * makers' power savers are harsh). Exempting the app keeps alerts instant; it costs
 * little, since the connection is idle until your computer has something to say.
 */
object BatteryExemption {
    fun granted(context: Context): Boolean =
        context.getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(context.packageName)

    /** Android's own "let this app run in the background?" question. */
    @SuppressLint("BatteryLife")
    fun request(context: Context) {
        val ask = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + context.packageName))
        runCatching { context.startActivity(ask) }.onFailure {
            runCatching { context.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) }
        }
    }
}
