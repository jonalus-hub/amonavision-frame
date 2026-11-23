# Volver atrás

Elige el nivel según lo que haya fallado. Ninguno borra fotos, la base de datos ni los logs.

## Nivel 1: volver a ffpyplayer (en segundos)

Es suficiente si mpv da problemas pero el resto del código va bien.

```bash
nano ~/AmonaVision/device.conf      # en [video]: backend = ffpyplayer  (o borra la sección [video])
sudo systemctl restart amonavision
grep "Video backend" ~/AmonaVision/app/logs/viewer.log | tail -1
# → "Video backend: ffpyplayer (…)"
```

Con `backend = ffpyplayer` el visor se comporta exactamente como antes de este
cambio. mpv puede quedarse instalado: no se usa.

> Si el marco tiene el **overlay activado**, este cambio solo dura hasta el
> siguiente reinicio. Para que se quede, desactiva el overlay, haz el cambio y
> vuelve a activarlo (mira el punto 2 de INSTALL.md).

## Nivel 2: volver a la versión anterior del código

Para cuando el problema no es solo de mpv. Usa las copias del punto 1 de
INSTALL.md, con el `STAMP` que anotaste:

```bash
STAMP=AAAAMMDD-HHMM
sudo systemctl stop amonavision

tar xzf ~/backup-amonavision-$STAMP.tgz -C ~/AmonaVision
cp -a ~/device.conf.bak-$STAMP ~/AmonaVision/device.conf   # si había
cp -a ~/config.txt.bak-$STAMP ~/AmonaVision/config.txt
sudo cp ~/amonavision.service.bak-$STAMP /etc/systemd/system/amonavision.service
sudo systemctl daemon-reload

sudo systemctl start amonavision
cat ~/AmonaVision/VERSION       # → la versión de antes
```

Los ficheros que añadió la versión nueva (por ejemplo `app/video/`) se quedan en
el disco, pero el código anterior no los usa.

Si tu despliegue usa **releases con symlink** (un directorio por versión y un
enlace `current`), basta con apuntar el enlace a la anterior y reiniciar:

```bash
ln -sfn <ruta>/releases/<versión-anterior> <ruta>/current
sudo systemctl restart amonavision
```

Este repo no crea esa estructura, así que solo aplica si la montaste tú aparte.

## Nivel 3 (opcional): desinstalar mpv

No hace falta para volver atrás. Solo si quieres dejar el sistema como estaba:

```bash
sudo apt remove mpv      # con el overlay desactivado, si lo usas
```

## Después de volver atrás

- Guarda el log antes de que rote: `cp ~/AmonaVision/app/logs/viewer.log ~/viewer.log.mpv-fallo-$(date +%F)`
- y el último log de mpv: `cp /run/amonavision/mpv-last.log ~/` (está en RAM: se pierde al reiniciar).
