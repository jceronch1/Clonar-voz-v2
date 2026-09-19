# Guion multivoz + modelos de texto locales

Dos añadidos sobre la app de clonación de voz:

1. **Modelos de texto locales** servidos con llama.cpp, incluidos los que ya
   tienes descargados con Ollama. El modelo escribe la historia.
2. **Guion multivoz**: repartir las intervenciones entre varias voces de tu
   biblioteca y generar el cuento, la novela o el diálogo completo en un WAV.

---

## Cómo funciona por dentro

Ollama guarda cada modelo como un **blob GGUF estándar** en
`~/.ollama/models/blobs/sha256-…`, y un manifiesto JSON en `manifests/` dice qué
blob corresponde a cada nombre. La app lee esos manifiestos y le pasa el blob
directamente a `llama-server`, sin copiar nada ni depender de que Ollama esté
corriendo:

```bash
llama-server -m <blob de Ollama> --port 8081 -c 8192 --jinja \
             -ngl 99 --device Vulkan1
```

Después le habla por su API compatible con OpenAI
(`/v1/chat/completions`, en streaming), así que el texto aparece en la web
según se va escribiendo.

También detecta cualquier `.gguf` que dejes en `modelos-texto/`.

### Ojo: no todos los modelos de Ollama cargan en llama.cpp

Algunas variantes que empaqueta Ollama traen metadatos que llama.cpp todavía no
entiende. Probados en tu equipo:

| Modelo | Estado |
|---|---|
| `llama3.1:8b` | ✅ funciona, respeta el formato de guion |
| `qwen2.5:7b` | ✅ funciona |
| `gemma4:e4b-it-qat` · `gemma4:12b-it-qat` | ✅ funcionan |
| `ornith-1.5:9b` | ✅ funciona |
| `gemma3:1b` · `gemma3:270m` | ⚠️ cargan, pero son demasiado pequeños para respetar `[PERSONAJE]` |
| `qwen3.5:0.8b` · `qwen3.5:2b` · `qwen3.5:9b` | ❌ `key qwen35.rope.dimension_sections has wrong array length` |
| `qwen3-vl:2b` | ❌ falta una clave de metadatos |
| `glm-ocr:latest` | ❌ arquitectura `glmocr` desconocida |
| `embeddinggemma`, `nomic-embed-text`, `qwen3-embedding` | ❌ son de embeddings: no generan texto |
| Los `:cloud` | no tienen pesos locales, no aparecen en la lista |

El botón **Comprobar compatibilidad** intenta cargar el modelo y te enseña el
error real de llama.cpp. El resultado queda cacheado en `.compat-llm.json` y se
muestra en la lista (`✓ probado` / `✕ no carga`).

**Recomendado para escribir guiones: `llama3.1:8b`.**

---

## Uso

### 1. Cargar un modelo de texto

Pestaña **🎭 Guion multivoz** → tarjeta **Modelo de texto**. Elige el modelo, el
dispositivo y pulsa **Cargar modelo**. Tarda unos segundos (`llama3.1:8b` carga
en ~9 s en la RTX 4070).

### 2. Escribir la historia

Describe lo que quieres y pulsa **✨ Escribir guion**. El texto va apareciendo en
directo. Al terminar se convierte solo en intervenciones.

El modelo recibe instrucciones de devolver siempre este formato:

```
[NARRADOR] La tarde caía sobre el puerto y las gaviotas callaron de golpe.
[ELENA] Abuela, ¿tú viste alguna vez un barco así de grande?
[ABUELA] Uno igual no, pequeña. Pero vi cosas que no creerías.
```

Si ya tienes tu propio texto, **Pegar mi propio texto** → pégalo con ese formato
→ **Convertir en guion**. También entiende `PERSONAJE: frase`.

### 3. Repartir las voces

La tarjeta **Reparto** lista los personajes detectados y les asigna voces
automáticamente. Cambiar la voz ahí la aplica a todas las intervenciones de ese
personaje. Cada personaje tiene un color, que se ve en el borde de sus
intervenciones y en la línea de tiempo.

### 4. Ajustar el guion

Cada intervención se puede editar a mano:

| Control | Qué hace |
|---|---|
| Personaje | renombrar (se propaga al reparto) |
| Voz | cambiar solo esta intervención |
| Pausa | silencio en milisegundos **después** de esta intervención |
| ▶ | sintetiza y reproduce **solo** esta intervención, para tantearla |
| ↑ ↓ | reordenar |
| ✕ | eliminar |

**+ Añadir intervención** mete una nueva al final.

### 5. Generar

**🎬 Generar audio completo** sintetiza cada intervención con su voz y las une
con las pausas. Al terminar:

- reproductor con **línea de tiempo clicable** (salta a esa intervención, y se
  resalta la que suena)
- **WAV** completo
- **Subtítulos** `.srt` con los tiempos reales de cada intervención
- **Texto** del guion en `.txt`

Los guiones se guardan con **Guardar guion** y se recuperan desde el desplegable
de la tarjeta *Escribir la historia*.

---

## VRAM: el LLM y el modelo de voz no siempre caben juntos

Tu RTX 4070 tiene 8 GB. `llama3.1:8b` ocupa ~4,6 GB y el modelo de voz ~1,5 GB
más el contexto: juntos van muy justos, y con modelos mayores directamente no
caben.

El selector **Al sintetizar voz** de la tarjeta *Modelo de texto* controla esto:

| Opción | Comportamiento |
|---|---|
| **Liberar la GPU si hace falta** (por defecto) | descarga el modelo de texto solo cuando ambos irían a la misma GPU |
| **Liberar siempre** | lo descarga antes de cada síntesis |
| **Mantener el modelo cargado** | no lo toca; útil si el LLM va en CPU o si te sobra VRAM |

Cuando se libera, lo verás en el registro: *«Liberando 'llama3.1:8b' de la GPU
para dejar sitio al modelo de voz…»*. Volver a pulsar **Cargar modelo** lo
recupera.

Al cerrar el servidor se mata también el `llama-server`, para no dejar VRAM
ocupada.

---

## Rendimiento medido

En este equipo (RTX 4070 Laptop, Vulkan), con `llama3.1:8b`:

| Operación | Tiempo |
|---|---|
| Cargar `llama3.1:8b` en GPU | ~9 s |
| Escribir un cuento de ~130 palabras | ~7 s |
| Sintetizar 14 intervenciones (72 s de audio) | ~101 s |
| Sintetizar 8 intervenciones (49 s de audio) | ~60 s |

---

## Archivos nuevos

```
llm.py                  descubrimiento de modelos, lector GGUF y gestión de llama-server
static/guion.js         interfaz del guion multivoz
guiones/                guiones guardados (JSON)
modelos-texto/          .gguf sueltos que quieras usar (opcional)
.compat-llm.json        caché de qué modelos cargan
GUION-MULTIVOZ.md       este documento
```

En `app.py` se añadieron los endpoints `/api/llm/*` y `/api/guion*`, la síntesis
multivoz con tiempos, la generación de subtítulos y la coordinación de VRAM.

---

## API

| Método | Ruta | Para qué |
|---|---|---|
| `GET` | `/api/llm/modelos` | catálogo (Ollama + carpeta local) |
| `POST` | `/api/llm/comprobar` | intenta cargar un modelo y cachea el resultado |
| `POST` | `/api/llm/cargar` | arranca llama-server con ese modelo |
| `POST` | `/api/llm/parar` | lo descarga de memoria |
| `GET` | `/api/llm/estado` | modelo cargado, dispositivo, registro |
| `POST` | `/api/llm/guion` | escribe un guion (tarea con SSE) |
| `POST` | `/api/llm/analizar` | convierte texto pegado en segmentos |
| `GET` `POST` `DELETE` | `/api/guiones` | guiones guardados |
| `POST` | `/api/guion/sintetizar` | genera el audio multivoz (tarea con SSE) |

El progreso de las tareas se sigue por `/api/tarea/{id}/eventos`, igual que en el
modo simple.
