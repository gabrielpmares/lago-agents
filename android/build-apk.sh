#!/usr/bin/env bash
# Builds Lago Agents for Android and publishes it to your phones, which update themselves.
#
#   ./build-apk.sh [-m "what changed"]   bump the version, build the signed APK, publish it
#   ./build-apk.sh --no-publish          build only (same version; for testing)
#   ./build-apk.sh install               also install over USB (adb), e.g. the first time
#
# Publishing hands the APK to remote.py: paired phones hear of it within a second through
# their live connection, download it over Tailscale, check it, and install it over the
# app they have (pairing and settings stay). Without -m, the notes are the commit subjects
# since the last release.
#
# Every release must be signed with the same key, or Android refuses the update. The first
# run makes one in ~/.local/share/lago-agents/android/: back that folder up.
set -euo pipefail
cd "$(dirname "$0")"

PUBLISH=1
INSTALL=0
NOTES=""
while [ $# -gt 0 ]; do
    case "$1" in
        -m) NOTES="$2"; shift 2 ;;
        --no-publish) PUBLISH=0; shift ;;
        install) INSTALL=1; shift ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ -z "${JAVA_HOME:-}" ]; then
    for jdk in "$HOME"/.local/jdk/jdk-17* /usr/lib/jvm/java-17-openjdk-amd64 /usr/lib/jvm/java-21-openjdk-amd64; do
        [ -x "$jdk/bin/java" ] && export JAVA_HOME="$jdk" && break
    done
fi
[ -n "${JAVA_HOME:-}" ] || { echo "Install JDK 17 (sudo apt install openjdk-17-jdk) or set JAVA_HOME." >&2; exit 1; }
[ -f local.properties ] || echo "sdk.dir=${ANDROID_HOME:-$HOME/Android/Sdk}" > local.properties

KEYDIR="$HOME/.local/share/lago-agents/android"
KEYSTORE="$KEYDIR/release.jks"
if [ ! -f keystore.properties ]; then
    mkdir -p "$KEYDIR"
    chmod 700 "$KEYDIR"
    if [ ! -f "$KEYSTORE" ]; then
        PASS="$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"
        "$JAVA_HOME/bin/keytool" -genkeypair -keystore "$KEYSTORE" -storepass "$PASS" -keypass "$PASS" \
            -alias lago -keyalg EC -groupname secp256r1 -validity 10000 \
            -dname "CN=Lago Agents" >/dev/null
        chmod 600 "$KEYSTORE"
        printf 'storeFile=%s\nstorePassword=%s\nkeyAlias=lago\nkeyPassword=%s\n' "$KEYSTORE" "$PASS" "$PASS" > "$KEYDIR/keystore.properties"
        chmod 600 "$KEYDIR/keystore.properties"
        echo "Made a signing key: $KEYSTORE (back it up)."
    fi
    ln -sf "$KEYDIR/keystore.properties" keystore.properties
fi

prop() { sed -n "s/^$1=//p" version.properties; }
BUILD=$(prop build)
if [ "$PUBLISH" = 1 ]; then
    PUBLISHED=$(python3 -c "import sys; sys.path.insert(0, '..'); import remote; i = remote.app_info(); print(i['code'] if i else 0)")
    # Always past both the last published and the last built version: a phone may run a
    # build that was installed by hand and never published.
    NEXT=$(( (BUILD > PUBLISHED ? BUILD : PUBLISHED) + 1 ))
    sed -i "s/^build=.*/build=$NEXT/" version.properties
    BUILD=$NEXT
fi
NAME="$(prop major).$(prop minor).$BUILD"
echo "== Building Lago Agents $NAME (version $BUILD)"

if ! ./gradlew assembleRelease -q --console=plain; then
    echo "Build failed; version.properties says build=$BUILD, which is fine to reuse." >&2
    exit 1
fi
cp app/build/outputs/apk/release/app-release.apk LagoAgents.apk
echo "APK ready: $(pwd)/LagoAgents.apk"

if [ "$PUBLISH" = 1 ]; then
    if [ -z "$NOTES" ]; then
        LAST=$(git log -1 --format=%H -- version.properties 2>/dev/null || true)
        NOTES=$(git log --format='- %s' ${LAST:+$LAST..}HEAD -- . 2>/dev/null | head -12 || true)
    fi
    python3 ../lago/remote.py publish-app LagoAgents.apk "$BUILD" "$NAME" "${NOTES:-Improvements and fixes.}"
    echo "Commit android/version.properties to keep the version history in git."
fi

if [ "$INSTALL" = 1 ]; then
    "${ANDROID_HOME:-$HOME/Android/Sdk}/platform-tools/adb" install -r LagoAgents.apk
fi
