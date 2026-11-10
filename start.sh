#!/usr/bin/env bash
# =============================================================================
#  J.A.R.V.I.S. for Linux  -  run:  bash start.sh
#  Installs, only if missing:  system packages  ->  Jarvis's packages  ->  settings
#  Then asks for ONE free AI key (first time only), checks it works, and starts Jarvis.
#  (The Linux twin of start.bat + setup.ps1. Tested on Ubuntu; other apt distros should work.)
# =============================================================================
set -eu
cd "$(dirname "$0")"

say()  { printf '  %s\n' "$*"; }
step() { printf '\n  \033[36m[%s/3] %s\033[0m\n' "$1" "$2"; }
ok()   { printf '        \033[32mOK\033[0m  %s\n' "$*"; }
warn() { printf '        \033[33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\n  \033[31mSETUP ERROR: %s\033[0m\n  Fix the problem above and run  bash start.sh  again (finished steps are skipped).\n' "$*"; exit 1; }

printf '\n  \033[36m=============================================================\n'
printf '     J.A.R.V.I.S.  //  checking and installing everything\n'
printf '  =============================================================\033[0m\n'

# =============================================================================
# 1. System packages
# =============================================================================
step 1 'System packages'
# python3-venv: private Python environment      python3-gi(-cairo), gir1.2-gtk-3.0: on-screen overlay
# gir1.2-gst*, gstreamer1.0-pipewire: screen capture on Wayland      xclip: typing non-English text
# xdotool: active window + media keys on X11      xdg-utils: opening files      curl: checking your key
PACKAGES="python3 python3-venv python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-gstreamer-1.0
          gir1.2-gst-plugins-base-1.0 gstreamer1.0-pipewire gstreamer1.0-plugins-base xclip xdotool xdg-utils curl"
if command -v apt-get >/dev/null && command -v dpkg-query >/dev/null; then
    missing=""
    for p in $PACKAGES; do
        dpkg-query -W -f='${db:Status-Status}' "$p" 2>/dev/null | grep -qx installed || missing="$missing $p"
    done
    if [ -n "$missing" ]; then
        say "Installing:$missing"
        say "(your password is needed once, for the system package manager)"
        sudo apt-get update -qq || warn 'Could not refresh the package list - trying anyway'
        # shellcheck disable=SC2086
        sudo apt-get install -y -qq $missing || die "Package installation failed."
    fi
    ok 'System packages ready'
else
    warn "Not an apt-based system: make sure the equivalents of these are installed:"
    say  "    $(echo $PACKAGES)"
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die 'Python 3.10 or newer is needed.'

# =============================================================================
# 2. Private environment + all requirements
# =============================================================================
step 2 'Jarvis packages'
# --system-site-packages: the overlay and screen capture use the system's GTK/GStreamer bindings
if [ ! -x .venv/bin/python ] || ! grep -q 'include-system-site-packages = true' .venv/pyvenv.cfg 2>/dev/null \
   || ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    say 'Creating a private Python environment...'
    rm -rf .venv
    python3 -m venv --system-site-packages .venv || die 'Could not create the Python environment (.venv).'
fi
req_hash=$(sha1sum requirements.txt | cut -d' ' -f1)
if [ "$(cat .venv/.requirements.sha1 2>/dev/null)" != "$req_hash" ]; then
    say 'Installing everything Jarvis needs (about 1 minute the first time)...'
    .venv/bin/python -m pip install --disable-pip-version-check -q --upgrade pip
    .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt \
        || die 'Package installation failed - check your internet connection.'
    echo "$req_hash" > .venv/.requirements.sha1
fi
ok 'Packages ready'

# =============================================================================
# 3. Settings + your free AI key
# =============================================================================
step 3 'Your AI key'
[ -f .env ] || cp .env.example .env
chmod 600 .env   # your keys: readable by you only

get_env() { sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" .env | head -n1 | tr -d '[:space:]'; }
set_env() {   # replace the first KEY= line (commented or not), drop duplicates, or append
    if grep -qE "^[[:space:]]*#?[[:space:]]*$1[[:space:]]*=" .env; then
        awk -v k="$1" -v v="$2" '$0 ~ "^[[:space:]]*#?[[:space:]]*" k "[[:space:]]*=" { if (!d) print k "=" v; d = 1; next } { print }' \
            .env > .env.tmp && mv .env.tmp .env && chmod 600 .env
    else
        echo "$1=$2" >> .env
    fi
}
key_provider() {
    case $1 in
        gsk_*) echo GROQ_API_KEY ;; sk-or-*) echo OPENROUTER_API_KEY ;;
        ghp_*|github_pat_*) echo GITHUB_TOKEN ;; *) echo GEMINI_API_KEY ;;   # Gemini keys start with AIza... or AQ.
    esac
}
test_key() {   # prints ok / bad / unknown (no internet or a service hiccup -> we accept the key)
    local code   # the key goes in on stdin, so it never shows up in the process list
    case $1 in
        GEMINI_API_KEY) code=$(printf 'x-goog-api-key: %s\n' "$2" | curl -s -o /dev/null -w '%{http_code}' -m 15 -H @- \
                               https://generativelanguage.googleapis.com/v1beta/models) ;;
        GROQ_API_KEY) code=$(printf 'Authorization: Bearer %s\n' "$2" | curl -s -o /dev/null -w '%{http_code}' -m 15 -H @- \
                             https://api.groq.com/openai/v1/models) ;;
        OPENROUTER_API_KEY) code=$(printf 'Authorization: Bearer %s\n' "$2" | curl -s -o /dev/null -w '%{http_code}' -m 15 -H @- \
                                   https://openrouter.ai/api/v1/key) ;;
        *) echo unknown; return ;;
    esac
    case $code in 2??) echo ok ;; 400|401|403) echo bad ;; *) echo unknown ;; esac
}

have=""
for k in GEMINI_API_KEY GROQ_API_KEY OPENROUTER_API_KEY MISTRAL_API_KEY GITHUB_TOKEN; do
    [ -n "$(get_env $k)" ] && have="$have $k"
done
if [ -n "$have" ]; then
    ok "Key found ($(echo $have | tr ' ' ','))"
else
    echo
    say 'Jarvis needs ONE free AI key (takes 1 minute, no credit card):'
    say '   Gemini:  https://aistudio.google.com/apikey   (sign in with Google -> "Create API key")'
    say '   Groq:    https://console.groq.com/keys        (another free option)'
    echo
    for _ in 1 2 3 4 5; do
        read -r -p '  Paste your key here and press Enter (Ctrl+Shift+V pastes): ' k || exit 1
        k=$(printf '%s' "$k" | tr -d "[:space:]\"'")
        [ -n "$k" ] || { warn 'Nothing pasted. Copy your key, then press Ctrl+Shift+V in this window.'; continue; }
        name=$(key_provider "$k")
        say 'Checking the key...'
        r=$(test_key "$name" "$k")
        [ "$r" = bad ] && { warn 'That key was rejected. Make sure you copied the whole key, then try again.'; continue; }
        set_env "$name" "$k"
        [ "$r" = ok ] && ok 'Key works and is saved' || ok 'Key saved (could not verify it right now - that is usually fine)'
        have=$name
        break
    done
    if [ -z "$have" ]; then
        warn 'No working key yet. Opening the settings file: paste your key after GEMINI_API_KEY= , save, and run start.sh again.'
        xdg-open .env >/dev/null 2>&1 || say "Edit $(pwd)/.env"
        exit 2
    fi
fi

# =============================================================================
# Voice input works in Google Chrome (Firefox has no speech recognition; Chromium usually lacks Google's speech keys)
# =============================================================================
if ! command -v google-chrome >/dev/null && ! command -v google-chrome-stable >/dev/null \
   && ! command -v microsoft-edge >/dev/null && [ ! -f .venv/.no-chrome ]; then
    echo
    say 'Voice control ("Jarvis, ...") needs Google Chrome. Without it you can still type commands.'
    if command -v apt-get >/dev/null && [ "$(dpkg --print-architecture)" = amd64 ]; then
        read -r -p '  Install Google Chrome now? [Y/n] ' a || a=n
        if [ "${a:-y}" != n ] && [ "${a:-y}" != N ]; then
            tmp=$(mktemp -d)
            curl -fL --progress-bar -o "$tmp/chrome.deb" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
                && sudo apt-get install -y -qq "$tmp/chrome.deb" && ok 'Google Chrome installed' \
                || warn 'Chrome install failed - get it from https://www.google.com/chrome'
            rm -rf "$tmp"
        else
            touch .venv/.no-chrome
            say '  OK - I will not ask again (delete .venv/.no-chrome to be asked next time).'
        fi
    else
        say '  Get it from https://www.google.com/chrome'
        touch .venv/.no-chrome
    fi
fi

echo
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    say 'Wayland: the desktop will ask to share your screen and allow remote control.'
    say 'Pick your screen, switch ON "Allow remote interaction", and click Share. (Stop it any time from the top bar.)'
fi
printf '  \033[32mAll systems ready. Launching J.A.R.V.I.S. ...\033[0m\n'
exec .venv/bin/python server.py
