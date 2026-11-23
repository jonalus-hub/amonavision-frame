# Instalar mpv en el marco canario

Son pasos a mano, para hacerlos por SSH en el **canario** como el usuario del
marco (el que ejecuta `amonavision.service`). En los ejemplos, el código está en
`~/AmonaVision`.

Nada de esto toca los marcos del piloto.

## 1. Copias de seguridad (antes de nada)

```bash
STAMP=$(date +%Y%m%d-%H%M)
cat ~/AmonaVision/VERSION                      # anota qué versión había

# La versión actual del código. Sin fotos, base de datos, logs ni venv,
# que no cambian con esta prueba.
tar czf ~/backup-amonavision-$STAMP.tgz -C ~/AmonaVision \
    --exclude=./venv --exclude=./app/media --exclude=./app/db --exclude=./app/logs .

# device.conf (si existe), config.txt y la unidad systemd instalada
cp -a ~/AmonaVision/device.conf ~/device.conf.bak-$STAMP 2>/dev/null || echo "(no hay device.conf)"
cp -a ~/AmonaVision/config.txt ~/config.txt.bak-$STAMP
cp -a /etc/systemd/system/amonavision.service ~/amonavision.service.bak-$STAMP

ls -l ~/*$STAMP*
```

Guarda el valor de `STAMP`: [ROLLBACK.md](ROLLBACK.md) lo usa.

## 2. Sistema de archivos overlay (solo si el marco lo tiene activado)

Con el overlay activo, todo lo que se escribe en la tarjeta se pierde al
reiniciar, también `apt install` y los cambios en `device.conf`. Compruébalo:

```bash
findmnt -no FSTYPE /                 # "overlay" = activado · "ext4" = desactivado
grep -o 'boot=overlay' /proc/cmdline # sale algo = activado
```

**Si sale `ext4`, salta al punto 3.**

Si está activado, desactívalo y reinicia:

```bash
sudo raspi-config     # Performance Options → Overlay File System → No
                      # (y deja la partición de arranque como estaba)
sudo reboot
```

Después del reinicio, `findmnt -no FSTYPE /` tiene que decir `ext4`.

## 3. Instalar mpv y las herramientas del benchmark

```bash
sudo apt update
sudo apt install -y mpv sysstat v4l-utils
```

- `mpv`: el reproductor.
- `sysstat`: trae `pidstat`, que usa `bench.sh`.
- `v4l-utils`: trae `v4l2-ctl`, que se usa abajo para comprobar el decodificador.

## 4. Verificar

```bash
mpv --version | head -1
# → mpv 0.3x.x / 0.4x.x …

mpv --hwdec=help | grep -i v4l2m2m
# → v4l2m2m y v4l2m2m-copy en la lista

mpv --vo=help | grep -wE 'gpu|drm'
# → gpu y drm en la lista

v4l2-ctl --list-devices | grep -A4 bcm2835-codec
# → "bcm2835-codec-decode (platform:bcm2835-codec)" con /dev/video10 (el decodificador)

id -nG | tr ' ' '\n' | grep -xE 'video|render'
# → video y render: el usuario del marco necesita los dos para la pantalla y el decodificador
```

Si `bcm2835-codec` no aparece, el decodificador hardware no está disponible.
mpv funcionaría por software, sin ninguna ventaja: no sigas y apúntalo.

## 5. Desplegar `develop` y reinstalar la unidad

Despliega la rama `develop` en `~/AmonaVision` como haces siempre. La unidad
`amonavision.service` cambia en esta versión (`RuntimeDirectory=amonavision`), así
que reinstálala igual que lo hace `setup.sh` (`install_unit`):

```bash
cd ~/AmonaVision
sed -e "s#__APP_USER__#$USER#g" -e "s#__APP_DIR__#$HOME/AmonaVision#g" amonavision.service \
    | sudo tee /etc/systemd/system/amonavision.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart amonavision
ls -ld /run/amonavision          # → existe, del usuario del marco
```

Hasta aquí, el marco sigue reproduciendo con ffpyplayer: es el valor por defecto.

## 6. Volver a activar el overlay (solo si lo tenía)

Primero termina el punto 7 si ya quieres dejar mpv activado: con el overlay
puesto, los cambios en `device.conf` no sobreviven a un reinicio.

```bash
sudo raspi-config     # Performance Options → Overlay File System → Yes
sudo reboot
findmnt -no FSTYPE /  # → overlay
mpv --version | head -1   # sigue instalado
```

## 7. Activar mpv

Añade al final de `~/AmonaVision/device.conf`. Si no existe, créalo con la
sección `[device]` que corresponda; mira `device.conf.example`.

```ini
[video]
backend = mpv
# Opcional, para probar variantes sin tocar el código (valores por defecto):
#mpv_hwdec = v4l2m2m-copy
#mpv_vo = gpu
#mpv_gpu_context = drm
#mpv_audio_device = pulse
#mpv_drm_mode = auto
#mpv_start_timeout = 5
#mpv_end_grace = 10
```

```bash
sudo systemctl restart amonavision
grep "Video backend" ~/AmonaVision/app/logs/viewer.log | tail -3
# → "Video backend: mpv (from device.conf): hwdec=v4l2m2m-copy, vo=gpu, …"
```

Si pone `mpv selected but 'mpv' is not installed`, el punto 3 no se completó.
Mientras tanto, los vídeos se siguen viendo con ffpyplayer.
