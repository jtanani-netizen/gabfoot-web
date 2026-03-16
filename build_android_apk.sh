#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$PROJECT_DIR/android_app"
BUILD_DIR="$APP_DIR/build"
GEN_DIR="$BUILD_DIR/gen"
CLASSES_DIR="$BUILD_DIR/classes"
DEX_DIR="$BUILD_DIR/dex"
DIST_DIR="$PROJECT_DIR/dist"
DESKTOP_DIR="/home/jibril/Bureau/gabi projet"
SIGNING_DIR="$DESKTOP_DIR/android-signing"
SIGNING_ENV="$SIGNING_DIR/signing.env"
KEYSTORE="$SIGNING_DIR/gabfoot-release.jks"

ANDROID_ROOT="/home/jibril/android-toolchain/android-sdk"
JAVA_HOME="/home/jibril/android-toolchain/jdk/jdk-17.0.18+8"
BUILD_TOOLS="$ANDROID_ROOT/build-tools/35.0.0"
ANDROID_JAR="$ANDROID_ROOT/platforms/android-35/android.jar"

AAPT="$BUILD_TOOLS/aapt"
ZIPALIGN="$BUILD_TOOLS/zipalign"
APKSIGNER="$BUILD_TOOLS/apksigner"
D8="$BUILD_TOOLS/d8"
JAVAC="$JAVA_HOME/bin/javac"
KEYTOOL="$JAVA_HOME/bin/keytool"

UNALIGNED_APK="$BUILD_DIR/GABFOOT-unaligned.apk"
ALIGNED_APK="$BUILD_DIR/GABFOOT-aligned.apk"
FINAL_APK="$DIST_DIR/GABFOOT.apk"
DESKTOP_APK="$DESKTOP_DIR/GABFOOT.apk"

mkdir -p "$BUILD_DIR" "$GEN_DIR" "$CLASSES_DIR" "$DEX_DIR" "$DIST_DIR" "$SIGNING_DIR"
rm -rf "$GEN_DIR" "$CLASSES_DIR" "$DEX_DIR"
mkdir -p "$GEN_DIR" "$CLASSES_DIR" "$DEX_DIR"

if [[ ! -f "$SIGNING_ENV" ]]; then
  STORE_PASS="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(18))
PY
)"
  cat > "$SIGNING_ENV" <<EOF
KEY_ALIAS=gabfoot
STORE_PASS=$STORE_PASS
KEY_PASS=$STORE_PASS
EOF
  chmod 600 "$SIGNING_ENV"
fi

set -a
source "$SIGNING_ENV"
set +a

KEY_PASS="$STORE_PASS"

if [[ ! -f "$KEYSTORE" ]]; then
  "$KEYTOOL" -genkeypair \
    -keystore "$KEYSTORE" \
    -storetype JKS \
    -storepass "$STORE_PASS" \
    -keypass "$KEY_PASS" \
    -alias "$KEY_ALIAS" \
    -dname "CN=GABFOOT, OU=Mobile, O=GABFOOT, L=Casablanca, ST=Casablanca-Settat, C=MA" \
    -keyalg RSA \
    -keysize 2048 \
    -validity 10000
fi

"$AAPT" package -f -m \
  -J "$GEN_DIR" \
  -M "$APP_DIR/AndroidManifest.xml" \
  -S "$APP_DIR/res" \
  -I "$ANDROID_JAR"

mapfile -t JAVA_FILES < <(find "$APP_DIR/src" "$GEN_DIR" -name '*.java' | sort)
if [[ "${#JAVA_FILES[@]}" -eq 0 ]]; then
  echo "Aucune source Java trouvee"
  exit 1
fi

"$JAVAC" \
  -encoding UTF-8 \
  -source 8 \
  -target 8 \
  -bootclasspath "$ANDROID_JAR" \
  -d "$CLASSES_DIR" \
  "${JAVA_FILES[@]}"

env JAVA_HOME="$JAVA_HOME" PATH="$JAVA_HOME/bin:$PATH" \
  "$D8" --lib "$ANDROID_JAR" --min-api 24 --output "$DEX_DIR" $(find "$CLASSES_DIR" -name '*.class' | sort)

"$AAPT" package -f \
  -M "$APP_DIR/AndroidManifest.xml" \
  -S "$APP_DIR/res" \
  -I "$ANDROID_JAR" \
  -F "$UNALIGNED_APK"

zip -q -j "$UNALIGNED_APK" "$DEX_DIR/classes.dex"
"$ZIPALIGN" -f 4 "$UNALIGNED_APK" "$ALIGNED_APK"

env JAVA_HOME="$JAVA_HOME" PATH="$JAVA_HOME/bin:$PATH" \
  "$APKSIGNER" sign \
  --ks "$KEYSTORE" \
  --ks-key-alias "$KEY_ALIAS" \
  --ks-pass "pass:$STORE_PASS" \
  --key-pass "pass:$KEY_PASS" \
  --out "$FINAL_APK" \
  "$ALIGNED_APK"

env JAVA_HOME="$JAVA_HOME" PATH="$JAVA_HOME/bin:$PATH" \
  "$APKSIGNER" verify "$FINAL_APK"
cp "$FINAL_APK" "$DESKTOP_APK"

echo "APK_OK $FINAL_APK"
