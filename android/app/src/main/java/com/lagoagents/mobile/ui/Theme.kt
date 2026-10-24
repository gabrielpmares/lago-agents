package com.lagoagents.mobile.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

// The desktop app's design tokens (ui/styles.css): one accent, lake green.
private val Dark = darkColorScheme(
    primary = Color(0xFF4FB89E), onPrimary = Color(0xFF0A1D18),
    primaryContainer = Color(0x214FB89E), onPrimaryContainer = Color(0xFF7ED9BF),
    secondary = Color(0xFFA6AAB1), onSecondary = Color(0xFF0F1012),
    background = Color(0xFF0F1012), onBackground = Color(0xFFE8E9EB),
    surface = Color(0xFF16181B), onSurface = Color(0xFFE8E9EB),
    surfaceVariant = Color(0xFF1C1F23), onSurfaceVariant = Color(0xFFA6AAB1),
    surfaceContainer = Color(0xFF16181B), surfaceContainerHigh = Color(0xFF1C1F23),
    surfaceContainerHighest = Color(0xFF23272C), surfaceContainerLow = Color(0xFF131416),
    outline = Color(0xFF363A42), outlineVariant = Color(0xFF272A30),
    error = Color(0xFFE26D69), onError = Color(0xFF1A0605),
    tertiary = Color(0xFFD6A54A),
)

private val Light = lightColorScheme(
    primary = Color(0xFF1C7662), onPrimary = Color(0xFFF3FBF8),
    primaryContainer = Color(0x1A1C7662), onPrimaryContainer = Color(0xFF166554),
    secondary = Color(0xFF4B5058), onSecondary = Color(0xFFFCFCFC),
    background = Color(0xFFF4F5F6), onBackground = Color(0xFF16181B),
    surface = Color(0xFFFCFCFC), onSurface = Color(0xFF16181B),
    surfaceVariant = Color(0xFFEEF0F2), onSurfaceVariant = Color(0xFF4B5058),
    surfaceContainer = Color(0xFFFCFCFC), surfaceContainerHigh = Color(0xFFEEF0F2),
    surfaceContainerHighest = Color(0xFFE5E8EB), surfaceContainerLow = Color(0xFFF7F8F9),
    outline = Color(0xFFCDD1D7), outlineVariant = Color(0xFFE0E3E7),
    error = Color(0xFFBB3A36), onError = Color(0xFFFFFFFF),
    tertiary = Color(0xFF8F6208),
)

private val Type = Typography(
    headlineSmall = TextStyle(fontSize = 24.sp, fontWeight = FontWeight.SemiBold, letterSpacing = (-0.4).sp),
    titleLarge = TextStyle(fontSize = 20.sp, fontWeight = FontWeight.SemiBold, letterSpacing = (-0.2).sp),
    titleMedium = TextStyle(fontSize = 16.sp, fontWeight = FontWeight.SemiBold),
    bodyLarge = TextStyle(fontSize = 16.sp, lineHeight = 23.sp),
    bodyMedium = TextStyle(fontSize = 14.sp, lineHeight = 20.sp),
    labelLarge = TextStyle(fontSize = 14.sp, fontWeight = FontWeight.SemiBold),
)

@Composable
fun LagoTheme(dark: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (dark) Dark else Light, typography = Type, content = content)
}
