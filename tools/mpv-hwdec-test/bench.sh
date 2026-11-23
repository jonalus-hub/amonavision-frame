#!/usr/bin/env bash
# Benchmark de reproducción de vídeo en el marco: el mismo vídeo con
#   a) ffpyplayer, como lo reproduce hoy el visor (ffpyplayer_bench.py)
#   b) mpv --hwdec=no            --vo=gpu --gpu-context=drm
#   c) mpv --hwdec=v4l2m2m-copy  --vo=gpu --gpu-context=drm
#   d) mpv --hwdec=v4l2m2m-copy  --vo=drm
# y para cada uno mide: % de CPU (pidstat -u 1), RAM (RSS, pidstat -r),
# temperatura (vcgencmd measure_temp cada 5 s), vcgencmd get_throttled al
# final y, en mpv, los fotogramas perdidos y si usó el decodificador hardware.
# Deja los datos en bruto por variante y un resumen en summary.csv y summary.txt.
#
# Este script NO para, arranca ni reinicia nada: te pide que pares el servicio
# a mano antes y que lo arranques a mano después.
#
# Uso (como el usuario del marco, en el directorio de AmonaVision):
#   tools/mpv-hwdec-test/bench.sh VIDEO [--seconds N] [--cooldown S] [--no-audio]
#                                       [--only a,c] [--out DIR]
#   VIDEO        un vídeo ya procesado por el marco, p. ej. app/media/processed/xxx.mp4
#   --seconds N  reproduce solo N segundos de cada variante (por defecto, el vídeo entero)
#   --cooldown S espera S segundos entre variantes para que baje la temperatura (por defecto 60)
#   --no-audio   sin sonido (por defecto suena por la salida HDMI, como en el marco)
#   --only       solo esas variantes (a, b, c, d)
#   --out DIR    dónde dejar los resultados (por defecto ~/mpv-bench-FECHA)
set -uo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$KIT_DIR/../.." && pwd)"
VENV_PY="$APP_DIR/venv/bin/python"

VIDEO=""
LIMIT=""
COOLDOWN=60
AUDIO=1
ONLY="a,b,c,d"
OUT="$HOME/mpv-bench-$(date +%Y%m%d-%H%M%S)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --seconds)  LIMIT="$2"; shift 2 ;;
        --cooldown) COOLDOWN="$2"; shift 2 ;;
        --no-audio) AUDIO=0; shift ;;
        --only)     ONLY="$2"; shift 2 ;;
        --out)      OUT="$2"; shift 2 ;;
        -h|--help)  sed -n '2,25p' "$0"; exit 0 ;;
        -*)         echo "Opción desconocida: $1" >&2; exit 2 ;;
        *)          VIDEO="$1"; shift ;;
    esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
[[ -n "$VIDEO" ]] || die "falta el vídeo de referencia (bench.sh VIDEO ...)"
[[ -f "$VIDEO" ]] || die "no existe: $VIDEO"
VIDEO="$(cd "$(dirname "$VIDEO")" && pwd)/$(basename "$VIDEO")"

# --- requisitos -------------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || die "falta '$1' — instálalo con: $2"; }
need mpv "sudo apt install mpv (ver INSTALL.md)"
need pidstat "sudo apt install sysstat"
need vcgencmd "viene con Raspberry Pi OS (paquete libraspberrypi-bin / raspi-utils)"
need python3 "sudo apt install python3"
[[ -x "$VENV_PY" ]] || die "no encuentro el venv de la app en $VENV_PY"

# --- el servicio tiene que estar parado (lo paras tú) -------------------------
viewer_running() { pgrep -f "viewer[.]py" >/dev/null 2>&1; }
if systemctl is-active --quiet amonavision || viewer_running; then
    echo "El servicio amonavision está en marcha y ocupa la pantalla."
    echo "Páralo tú en otra terminal:   sudo systemctl stop amonavision"
    read -r -p "Pulsa Enter cuando esté parado (Ctrl+C para cancelar)... " _
    if systemctl is-active --quiet amonavision || viewer_running; then
        die "sigue en marcha; no continúo"
    fi
fi

# --- pantalla y sonido: los mismos que usa el marco -------------------------
HAL_ENV="$(cd "$APP_DIR" && python3 -m app.hal env 2>/dev/null)" && eval "$HAL_ENV"
CARD="${AV_KMSDRM_DEVICE_INDEX:-1}"
CONNECTOR="${AV_HDMI_CONNECTOR:-}"
export SDL_VIDEODRIVER=kmsdrm SDL_KMSDRM_DEVICE_INDEX="$CARD" SDL_AUDIODRIVER=pulse
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PULSE_RUNTIME_PATH="$XDG_RUNTIME_DIR/pulse"
export PULSE_LATENCY_MSEC=150

MPV_COMMON=(--fs --no-osc --no-osd-bar --no-input-default-bindings --input-terminal=no
            --really-quiet --no-config --input-vo-keyboard=no --keep-open=no --idle=no
            --osd-level=0 --cursor-autohide=always "--drm-device=/dev/dri/card$CARD")
[[ -n "$CONNECTOR" ]] && MPV_COMMON+=("--drm-connector=$CONNECTOR")
[[ -n "$LIMIT" ]] && MPV_COMMON+=("--length=$LIMIT")
if [[ $AUDIO -eq 1 ]]; then MPV_COMMON+=(--audio-device=pulse); else MPV_COMMON+=(--no-audio); fi
FFP_ARGS=()
[[ -n "$LIMIT" ]] && FFP_ARGS+=(--seconds "$LIMIT")
[[ $AUDIO -eq 0 ]] && FFP_ARGS+=(--no-audio)

mkdir -p "$OUT" || die "no puedo crear $OUT"
# Los sockets IPC de mpv van en una ruta corta (un socket Unix admite ~108 caracteres)
SOCK_DIR="$(mktemp -d /tmp/mpvbench.XXXXXX)" || die "no puedo crear un directorio en /tmp"
trap 'rm -rf "$SOCK_DIR"' EXIT
{
    echo "fecha: $(date -Is)"
    echo "vídeo: $VIDEO"
    echo "pantalla: /dev/dri/card$CARD ${CONNECTOR:-(mpv elige)}"
    echo "mpv: $(mpv --version 2>/dev/null | head -1)"
    echo "kernel: $(uname -r)"
    echo "segundos por variante: ${LIMIT:-vídeo entero} · enfriado: ${COOLDOWN} s · audio: $AUDIO"
} > "$OUT/info.txt"

temp_c() { vcgencmd measure_temp 2>/dev/null | sed -E "s/[^0-9.]//g"; }

# run_variant ID PLAYER HWDEC VO -- comando...
run_variant() {
    local id="$1" player="$2" hwdec="$3" vo="$4"; shift 5
    local dir="$OUT/$id-$player${hwdec:+-$hwdec}${vo:+-$vo}"
    mkdir -p "$dir"
    echo
    echo "=== [$id] $player ${hwdec:+hwdec=$hwdec }${vo:+vo=$vo}"
    echo "    enfriando ${COOLDOWN} s (ahora $(temp_c) °C)..."
    sleep "$COOLDOWN"

    local start_t; start_t="$(temp_c)"
    echo "$(date +%s) $start_t" > "$dir/temps.txt"
    "$@" > "$dir/player.out" 2> "$dir/player.err" &
    local pid=$!
    local stats_pid=""
    if [[ "$player" == "mpv" ]]; then
        python3 "$KIT_DIR/mpv_stats.py" "$SOCK_DIR/$id.sock" > "$dir/stats.json" 2>/dev/null &
        stats_pid=$!
    fi
    LC_ALL=C pidstat -h -u -r -p "$pid" 1 > "$dir/pidstat.txt" 2>&1 &
    local pidstat_pid=$!
    ( while kill -0 "$pid" 2>/dev/null; do sleep 5; echo "$(date +%s) $(temp_c)" >> "$dir/temps.txt"; done ) &
    local temps_pid=$!

    wait "$pid"; local rc=$?
    kill "$pidstat_pid" 2>/dev/null; wait "$pidstat_pid" 2>/dev/null
    wait "$temps_pid" 2>/dev/null
    [[ -n "$stats_pid" ]] && { wait "$stats_pid" 2>/dev/null || true; }
    echo "$(date +%s) $(temp_c)" >> "$dir/temps.txt"
    [[ "$player" == "ffpyplayer" ]] && cp "$dir/player.out" "$dir/stats.json"
    local throttled; throttled="$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
    printf '{"variant": "%s", "player": "%s", "hwdec": "%s", "vo": "%s", "exit_code": %d, "throttled_end": "%s"}\n' \
        "$id" "$player" "$hwdec" "$vo" "$rc" "$throttled" > "$dir/meta.json"
    echo "    terminado: código de salida $rc, throttled=$throttled, $(temp_c) °C"
}

mpv_variant() {   # ID HWDEC VO [opciones extra...]
    local id="$1" hwdec="$2" vo="$3"; shift 3
    local dir="$OUT/$id-mpv-$hwdec-$vo"
    run_variant "$id" mpv "$hwdec" "$vo" -- \
        mpv "${MPV_COMMON[@]}" "--hwdec=$hwdec" "--vo=$vo" "$@" \
            "--input-ipc-server=$SOCK_DIR/$id.sock" "--log-file=$dir/mpv.log" -- "$VIDEO"
}

IFS=',' read -r -a SELECTED <<< "$ONLY"
for id in "${SELECTED[@]}"; do
    case "$id" in
        a) run_variant a ffpyplayer "" "" -- \
               "$VENV_PY" "$KIT_DIR/ffpyplayer_bench.py" "$VIDEO" "${FFP_ARGS[@]}" ;;
        b) mpv_variant b no gpu --gpu-context=drm ;;
        c) mpv_variant c v4l2m2m-copy gpu --gpu-context=drm ;;
        d) mpv_variant d v4l2m2m-copy drm ;;
        *) echo "variante desconocida: $id (a, b, c, d)" >&2 ;;
    esac
done

echo
python3 "$KIT_DIR/summarize.py" "$OUT"
echo "Resultados en: $OUT  (summary.csv, summary.txt y una carpeta por variante)"
echo
echo "Cuando acabes, arranca el servicio tú:   sudo systemctl start amonavision"
