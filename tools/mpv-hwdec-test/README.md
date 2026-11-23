# Kit de pruebas: vídeo con mpv y decodificación por hardware

Este kit sirve para probar en el **marco canario** el backend de vídeo con mpv
(`device.conf` → `[video] backend = mpv`). Con ese backend, mpv decodifica el
H.264 con el hardware de la Pi 4 (V4L2 M2M) y pinta el vídeo él mismo, en vez
de que pygame copie y escale cada fotograma.

> **Orden obligatorio**
> 1. Primero se prueba **solo en el canario**, con la rama `develop`.
> 2. Después, **7 días en remojo** en el canario, con uso normal.
> 3. **Nada pasa a `stable` antes de que termine el piloto.** Los marcos del
>    piloto no se tocan.

## Qué hay aquí

| Fichero | Para qué |
|---|---|
| [INSTALL.md](INSTALL.md) | Instalar mpv en el canario: copias de seguridad, overlay si lo hay, verificación y cómo activar mpv |
| `bench.sh` | Comparar ffpyplayer con tres variantes de mpv en el mismo vídeo: CPU, RAM, temperatura, throttling y fotogramas perdidos |
| [CHECKLIST.md](CHECKLIST.md) | Pruebas a mano con el marco: botones, vídeos seguidos, recuperación, heartbeat… |
| [ROLLBACK.md](ROLLBACK.md) | Volver atrás: a ffpyplayer en segundos, o a la versión anterior del código |
| `ffpyplayer_bench.py`, `mpv_stats.py`, `summarize.py` | Ayudantes de `bench.sh` (no hace falta llamarlos a mano) |

## Pasos

1. Sigue los puntos 1 a 5 de [INSTALL.md](INSTALL.md), sin activar todavía mpv en `device.conf`.
   Si el marco usa overlay, déjalo **desactivado mientras dure la prueba**: así no se pierden
   los cambios de `device.conf` ni los resultados del benchmark al reiniciar.
   Vuelve a activarlo al final (punto 6).
2. Para el servicio a mano y lanza el benchmark con un vídeo real del marco:
   ```bash
   sudo systemctl stop amonavision
   cd ~/AmonaVision
   tools/mpv-hwdec-test/bench.sh app/media/processed/<un-video>.mp4 --seconds 120
   sudo systemctl start amonavision
   ```
   El script no para ni arranca el servicio por su cuenta: si lo encuentra en
   marcha, te pide que lo pares tú. Cada variante tarda lo que dure el vídeo
   (o `--seconds`) más 60 s de enfriado (`--cooldown`). Los resultados quedan en
   `~/mpv-bench-FECHA/`: `summary.txt` para leer y `summary.csv` para comparar.
3. Activa mpv (punto 7 de INSTALL.md) con la variante que mejor salga en el benchmark.
4. Haz las pruebas de [CHECKLIST.md](CHECKLIST.md).
5. Déjalo 7 días con uso normal. Revisa de vez en cuando los avisos de mpv en el log:
   ```bash
   grep -E "Video backend|mpv:|Display back" ~/AmonaVision/app/logs/viewer.log | tail -50
   ```
6. Si algo va mal: [ROLLBACK.md](ROLLBACK.md).

## Dónde se ve qué

- **Log del visor:** `~/AmonaVision/app/logs/viewer.log`. Las líneas `Video backend: …`
  dicen qué reproductor se usó y por qué. Las líneas `mpv: …` muestran el arranque, el
  primer fotograma, `hwdec-current`, el final, el código de salida, los fotogramas
  perdidos y los fallos.
- **Heartbeat del visor:** `/run/amonavision/viewer-heartbeat` contiene `<hora unix> <estado>`
  y se reescribe cada 2 s como máximo, también durante un vídeo de mpv (`PLAYING/mpv`).
  Si la unidad todavía no se ha reinstalado, está en `/run/user/<uid>/amonavision/`.
- **Último log de mpv:** `/run/amonavision/mpv-last.log`. Se sobrescribe en cada vídeo.
