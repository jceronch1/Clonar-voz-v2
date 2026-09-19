# Dictado por micrófono en «Voz simple»

Hablas por el micrófono, haces una pausa, y la app transcribe y **genera el audio
sola**, sin tocar el botón. Todo en local, con Whisper.

---

## Cómo se usa

En **🎤 Voz simple**, arriba del cuadro de texto, pulsa **🎙️ Dictar**.

1. Calibra el ruido de fondo durante medio segundo.
2. Se pone a escuchar. El medidor muestra tu nivel y una marca blanca con el
   umbral a partir del cual considera que estás hablando.
3. Hablas. El estado pasa a *«Te escucho…»*.
4. Callas. Cuando el silencio dura más que la **pausa configurada** (1,2 s por
   defecto), cierra la frase, la transcribe y lanza la síntesis.
5. Con **modo continuo**, en cuanto termina de sonar el audio generado vuelve a
   escuchar. Hablas otra vez y se repite.

El botón ⚙ abre los ajustes.

| Ajuste | Qué hace |
|---|---|
| **Modelo de voz a texto** | `small` (460 MB) va sobrado y es rápido; `large-v3` es más preciso pero más lento |
| **Procesamiento** | automático usa CUDA si está disponible; si no, CPU |
| **Pausa que cierra la frase** | 400–3000 ms. Si te come el final, súbela; si tarda en reaccionar, bájala |
| **Sensibilidad** | veces por encima del ruido de fondo. Más baja capta voz más floja pero también más ruido |
| **Generar automáticamente** | si lo apagas, solo transcribe y va acumulando texto |
| **Seguir escuchando** | el bucle continuo |
| **Comandos hablados** | ver abajo |
| **Repasar los signos con el modelo de texto** | pasada extra con el LLM (opcional) |

Con **generar automáticamente activado**, cada frase sustituye el texto anterior:
dices una cosa, la oyes con tu voz clonada, dices otra. Si lo **apagas**, las
frases se van **acumulando** en el cuadro, que es lo cómodo para dictar un texto
largo y generarlo de una vez al final.

---

## Los signos de interrogación y exclamación

Esto importa de verdad: Qwen3-TTS usa `¿` y `¡` como pista de entonación. Si el
texto no los lleva, la voz generada suena plana. Hay cuatro capas:

### 1. Whisper con `initial_prompt`

Se le pasa un texto de ejemplo cargado de signos, que lo empuja a escribirlos.
De paso recupera mayúsculas y puntos finales.

### 2. Comandos hablados

Los dices y se convierten en el signo. Es la vía **fiable** cuando la entonación
no basta:

| Lo que dices | Lo que escribe |
|---|---|
| «abre interrogación» / «abrir interrogación» | `¿` |
| «cierra interrogación» / «signo de interrogación» | `?` |
| «abre exclamación» | `¡` |
| «cierra exclamación» / «signo de exclamación» | `!` |
| «coma» | `,` |
| «punto» / «punto y seguido» | `.` |
| «punto y aparte» | `.` + salto de línea |
| «nueva línea» / «salto de línea» | salto de línea |
| «dos puntos» | `:` |
| «punto y coma» | `;` |
| «puntos suspensivos» | `…` |
| «abre paréntesis» / «cierra paréntesis» | `(` `)` |

«coma» y «punto» solo cuentan como comando si no van detrás de un determinante,
así que *«nos vemos en el punto de encuentro»* o *«espero que coma bien»* se
escriben tal cual.

### 3. Normalizador automático

Si una frase acaba en `?` o `!` pero le falta el signo de apertura, se lo pone.
Lo coloca al principio de la oración: en *«Hola, ¿cómo estás?»* quedaría
*«¿Hola, cómo estás?»*. No es donde lo pondría un humano, pero el texto queda
bien formado y la entonación llega correcta al modelo de voz, que es lo que
buscamos.

### 4. Repaso con el modelo de texto (opcional)

Si tienes un modelo cargado en «Guion multivoz», esta casilla le pasa la
transcripción para que ajuste solo los signos. Hay un guardarraíl: se comparan
las palabras una a una (ignorando tildes y mayúsculas) y **si el modelo cambia,
añade o quita alguna, se descarta su versión**. Solo puede tocar puntuación,
tildes y mayúsculas.

Ejemplo real: *«Qué frío hace hoy, no tienes una manta»* → *«¿Qué frío hace hoy?
No tienes una manta.»*

### Qué esperar de verdad

Medido con frases sintetizadas:

- **Interrogaciones: muy fiables.** La entonación ascendente se detecta bien y
  `¿…?` sale casi siempre.
- **Exclamaciones: irregulares.** *«¡Esto va de maravilla!»* sale a menudo como
  *«Esto va de maravilla.»*. La entonación exclamativa es más sutil y ni Whisper
  ni el LLM (que solo ve texto) pueden recuperarla con garantías.

**Para asegurar una exclamación, dila:** «abre exclamación qué frío hace cierra
exclamación».

---

## Detalles del funcionamiento

**Detección de voz.** Corre en el navegador con Web Audio. Mide el nivel RMS
unas 60 veces por segundo y lo compara con el ruido de fondo, que se reestima
continuamente durante los silencios (el micrófono cambia de ganancia solo y un
umbral fijo acaba dejando de valer). Hay histéresis —cuesta más empezar a hablar
que seguir hablando, así no corta entre palabras—, un mínimo de 350 ms para que
un golpe no cuente como frase, y un tope de 30 s por si no callas nunca.

El control automático de ganancia del micrófono va **desactivado** a propósito:
sube el volumen en los silencios y hace bailar el umbral. La cancelación de eco
sí está activa.

**Nada de realimentación.** Mientras transcribe, genera y reproduce, deja de
escuchar. Solo vuelve cuando el audio generado ha terminado de sonar. Si no, se
oiría a sí misma y entraría en bucle.

**El audio nunca sale del equipo.** El navegador graba en WebM/Opus, se convierte
a WAV de 16 kHz con ffmpeg y se transcribe en local.

---

## Rendimiento medido

RTX 4070 Laptop, Whisper `small` en CUDA:

| Operación | Tiempo |
|---|---|
| Cargar el modelo | ~2,8 s (solo la primera vez) |
| Transcribir una frase de 3 s | ~0,6–1,0 s |
| Del final de tu frase al audio sonando | ~8 s (la síntesis es lo que manda) |

El modelo se carga en segundo plano al pulsar «Dictar», así que la primera frase
no espera. Se puede soltar con «Descargar el modelo de memoria».

---

## Archivos nuevos

```
dictado.py          faster-whisper, comandos hablados, normalizador y repaso con LLM
static/dictado.js   detección de voz en el navegador y bucle de dictado
DICTADO.md          este documento
```

En `app.py`: los endpoints `/api/dictado/*` y los ajustes `stt_*` / `dictado_*`.

| Método | Ruta | Para qué |
|---|---|---|
| `GET` | `/api/dictado/estado` | disponibilidad, CUDA, modelo cargado, ajustes |
| `POST` | `/api/dictado/cargar` | carga el modelo de Whisper |
| `POST` | `/api/dictado/parar` | lo suelta de memoria |
| `POST` | `/api/dictado/transcribir` | sube el audio y devuelve el texto ya puntuado |

## Requisitos

`faster-whisper` ya está instalado en este equipo, y los modelos `small` y
`large-v3` están en la caché de Hugging Face. En otra máquina haría falta:

```bash
pip install faster-whisper
```

Los modelos se descargan solos la primera vez que se usan.
