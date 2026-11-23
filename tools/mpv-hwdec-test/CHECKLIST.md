# Pruebas a mano en el canario (mpv activado)

**Antes de empezar**, deja dos terminales SSH abiertas:

```bash
# 1) el log del visor, filtrado
tail -F ~/AmonaVision/app/logs/viewer.log | grep --line-buffered -E "Video backend|mpv:|Display|GPIO: Button|error|Error"

# 2) el heartbeat del visor
watch -n1 cat /run/amonavision/viewer-heartbeat
```

Antes de cada vídeo, comprueba que `pgrep -a mpv` no devuelve nada.

| # | Prueba | Cómo | Qué tiene que pasar |
|---|---|---|---|
| 1 | Vídeo normal (horizontal) | Navega hasta un vídeo y pulsa **Play** | Vídeo a pantalla completa, con sonido por los altavoces de la pantalla. En el log: `Video backend: mpv for …`, `mpv: first frame after X s`, `hwdec-current=v4l2m2m-copy (hardware decoding: yes)`. Al acabar: `mpv: ended … exit code 0 … hardware decoding: yes`, `Display back from mpv in X s`. Vuelve la portada y el pase sigue. |
| 2 | Tiempo en negro | Cronometra en la prueba 1 | Anota los segundos en negro de Play al primer fotograma (también aparece en `first frame after X s`) y del final del vídeo a la portada (`Display back … in X s`). Sin parpadeos raros ni "No signal". La resolución de la pantalla no cambia. |
| 3 | Vídeo vertical | Reproduce un vídeo grabado en vertical | Se ve completo, centrado, con el fondo desenfocado a los lados. No sale estirado ni recortado. |
| 4 | **Play** a mitad de vídeo | Pulsa Play con el vídeo en marcha | El vídeo para enseguida y vuelve la portada. En el log no aparece ningún fallo de mpv. En `app/logs/buttons.csv`: `press, play` con estado `PLAYING`. |
| 5 | **Siguiente** a mitad de vídeo | Pulsa → | Para el vídeo y pasa a la siguiente sesión. `buttons.csv`: `next` con estado `PLAYING`. |
| 6 | **Anterior** a mitad de vídeo | Pulsa ← | Igual que la 5, hacia atrás. |
| 7 | **Grabar** a mitad de vídeo | Mantén pulsado Grabar | Para el vídeo y empieza a grabar; al soltar, se envía la nota de voz. ⚠ En AmonaVision3, el botón de grabar está desactivado mientras GPIO19 sea el WS del micro I2S: apúntalo como "no probado" hasta mover el pin. |
| 8 | Dos vídeos seguidos | Deja acabar un vídeo, ve al siguiente vídeo y pulsa Play; repítelo varias veces | Cada vídeo arranca con mpv; la pantalla va y vuelve cada vez sin quedarse en negro. Al terminar, `pgrep -a mpv` no devuelve nada. |
| 9 | Vídeo con nota de voz | Reproduce un vídeo que lleve una nota de voz | El vídeo sale sin su audio y suena la nota de voz (en la línea de mpv: `audio=off (paired voice note)`). |
| 10 | Llega una foto durante un vídeo | Manda una foto al grupo mientras suena un vídeo | El vídeo no se corta. La foto aparece después, en el pase normal. |
| 11 | **Matar mpv a mano** | Durante un vídeo: `pkill -9 -x mpv` | En menos de un segundo, la pantalla vuelve a pygame y **ese vídeo vuelve a empezar con ffpyplayer**. En el log: `mpv failed on … (mpv exited with code -9) — playing it with ffpyplayer`. Si vuelves a darle Play a ese mismo vídeo, sale con ffpyplayer (`mpv failed on this video before`) hasta que se reinicie el servicio. |
| 12 | Heartbeat durante el vídeo | Mira la terminal 2 durante un vídeo | `<hora> PLAYING/mpv`, con la hora avanzando cada ≤ 2 s. Si se para más de 5 s, el bucle del visor se ha bloqueado: es un fallo. |
| 13 | Atenuación nocturna | En el panel web, baja el brillo (p. ej. al 30 %) y pon un vídeo | Con `mpv_vo = gpu`: el vídeo sale atenuado igual que las fotos (`dimmed x0.30` en la línea `mpv: starting`). Con `mpv_vo = drm`: se reproduce con ffpyplayer (`can't dim`). |
| 14 | Fallback si falta mpv | En `device.conf` pon `mpv_binary = /no/existe` y reinicia el servicio | Log: `mpv selected but '/no/existe' is not installed`. Los vídeos funcionan con ffpyplayer. Luego quita la línea. |
| 15 | Fallback por timeout | Pon `mpv_start_timeout = 0.1` y reinicia | El vídeo no llega a salir con mpv: `no first frame within 0.1 s`, y se ve con ffpyplayer. Luego quita la línea. |
| 16 | CPU y temperatura | Durante un vídeo largo: `top -d 2` y `vcgencmd measure_temp` | mpv bastante por debajo de lo que gastaba `python viewer.py` con ffpyplayer (compáralo con `bench.sh`). La temperatura no sube como antes. |
| 17 | Reinicio durante un vídeo | Manda `/restart` al bot mientras suena un vídeo | El servicio reinicia limpio. `pgrep -a mpv` no deja ningún mpv huérfano. |
| 18 | 7 días en remojo | Uso normal | Revisa cada pocos días: `grep -cE "mpv failed|could not start|Could not take the screen back" viewer.log` tiene que dar 0 o casi 0. Sin reinicios inesperados: `journalctl -u amonavision --since -7d \| grep -c Started`. |

Anota para cada prueba: OK, fallo (con la línea del log) o no probado.
