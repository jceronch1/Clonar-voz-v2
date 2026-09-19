"use strict";

const $ = (id) => document.getElementById(id);
const api = async (url, opciones) => {
  const r = await fetch(url, opciones);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch { /* respuesta sin JSON */ }
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
};

const estado = {
  sistema: null,
  voces: [],
  vozSeleccionada: null,
  tareaActual: null,
  fuenteEventos: null,
  audioGrabado: null,
};

// Superficie compartida con guion.js, que se carga después
const App = (window.App = {
  api: (...a) => api(...a),
  estado,
  alCambiarVoces: [],        // callbacks que se disparan al recargar la biblioteca
  alCargarEstado: [],
  alTerminarGeneracion: [],  // se dispara al acabar (o fallar) una síntesis
});

const CAMPOS_AJUSTES = ["temp", "top_p", "top_k", "semilla",
                        "chars_por_bloque", "pausa_ms", "max_frames", "hilos"];

/* ======================= Estado del sistema ======================= */
function pastilla(punto, etiqueta, valor, titulo = "") {
  return `<span class="pastilla" title="${titulo.replace(/"/g, "&quot;")}">
            <span class="punto ${punto}"></span>${etiqueta} <b>${valor}</b>
          </span>`;
}

async function cargarEstado() {
  const s = await api("/api/estado");
  estado.sistema = s;

  const modelo = s.modelo ? s.modelo.split(/[\\/]/).pop() : "no encontrado";
  const gpus = s.dispositivos.filter((d) => !d.id.toLowerCase().startsWith("cpu"));

  $("pastillas").innerHTML = [
    pastilla(s.binario_ok && s.soporta_qwen3tts ? "ok" : "mal", "llama.cpp",
             s.version || "no encontrado", s.binario),
    pastilla(s.modelo_ok && s.mmproj_ok ? "ok" : "mal", "Modelo",
             s.modelo_ok ? modelo : "falta", s.modelo),
    pastilla(gpus.length ? "ok" : "medio", "Cómputo",
             gpus.length ? `${gpus.length} GPU · CPU` : "solo CPU",
             gpus.map((g) => `${g.id}: ${g.nombre}`).join("\n")),
    pastilla(s.ffmpeg ? "ok" : "medio", "ffmpeg", s.ffmpeg ? "sí" : "no"),
  ].join("");

  const problemas = [];
  if (!s.binario_ok) {
    problemas.push("No se encuentra <code>llama-tts</code>. Instálalo con " +
                   "<code>winget install ggml.llamacpp</code> o indica su ruta en config.json.");
  } else if (!s.soporta_qwen3tts) {
    problemas.push("Tu build de llama.cpp es anterior al soporte de Qwen3-TTS. " +
                   "Actualiza con <code>winget upgrade ggml.llamacpp</code>.");
  }
  // Si falta el modelo, el panel de descarga ya lo explica: no duplicamos el aviso.
  const faltaModelo = !s.modelo_ok || !s.mmproj_ok;
  $("panel-modelo").classList.toggle("oculto", !faltaModelo);
  if (faltaModelo) await cargarCatalogoModelo();
  if (!s.ffmpeg) {
    problemas.push("Sin <code>ffmpeg</code> solo podrás subir referencias en wav o mp3, " +
                   "y la grabación desde el navegador no funcionará.");
  }

  const aviso = $("aviso");
  aviso.classList.toggle("oculto", problemas.length === 0);
  aviso.innerHTML = problemas.join("<br>");

  // Idiomas
  $("idioma").innerHTML = Object.entries(s.idiomas)
    .map(([cod, nom]) => `<option value="${cod}">${nom}</option>`).join("");
  $("idioma").value = s.config.idioma;

  // Dispositivos
  const opciones = [`<option value="auto">Automático (mejor disponible)</option>`];
  for (const d of s.dispositivos) {
    const marca = d.id === s.dispositivo_preferido ? " ★" : "";
    opciones.push(`<option value="${d.id}">GPU · ${d.nombre}${marca}</option>`);
  }
  opciones.push(`<option value="cpu">Solo CPU (${s.cpus} núcleos)</option>`);
  $("dispositivo").innerHTML = opciones.join("");
  $("dispositivo").value = s.config.dispositivo;

  // Ajustes avanzados
  for (const campo of CAMPOS_AJUSTES) {
    if ($(campo) && s.config[campo] !== undefined) $(campo).value = s.config[campo];
  }
  actualizarEtiquetasRango();
  App.alCargarEstado.forEach((f) => { try { f(s); } catch { /* ignora */ } });
}

/* ======================= Descarga del modelo ======================= */
async function cargarCatalogoModelo() {
  const c = await api("/api/modelo/catalogo");
  $("enlace-hf").href = c.url;

  const opciones = (grupo, pordefecto) => Object.entries(c.catalogo[grupo])
    .map(([clave, i]) =>
      `<option value="${clave}" ${clave === pordefecto ? "selected" : ""}>${i.etiqueta}</option>`)
    .join("");

  $("quant-modelo").innerHTML = opciones("modelo", c.por_defecto.modelo);
  $("quant-mmproj").innerHTML = opciones("mmproj", c.por_defecto.mmproj);
}

function descargaEnCurso(activa) {
  $("btn-descargar-modelo").disabled = activa;
  $("btn-descargar-modelo").textContent = activa ? "Descargando…" : "Descargar ahora";
  $("btn-cancelar-descarga").classList.toggle("oculto", !activa);
  $("quant-modelo").disabled = activa;
  $("quant-mmproj").disabled = activa;
}

async function descargarModelo() {
  descargaEnCurso(true);
  $("progreso-modelo").classList.remove("oculto");
  $("texto-modelo").textContent = "Conectando con Hugging Face…";
  $("barra-modelo").style.width = "0%";

  try {
    await api("/api/modelo/descargar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ modelo: $("quant-modelo").value, mmproj: $("quant-mmproj").value }),
    });
  } catch (err) {
    descargaEnCurso(false);
    $("texto-modelo").textContent = "✕ " + err.message;
    return;
  }

  const fuente = new EventSource("/api/modelo/progreso");
  fuente.onmessage = async (ev) => {
    const d = JSON.parse(ev.data);

    if (d.total) {
      const pct = (d.hecho / d.total) * 100;
      $("barra-modelo").style.width = pct.toFixed(1) + "%";
      const restante = d.velocidad > 0 ? (d.total - d.hecho) / d.velocidad : 0;
      $("texto-modelo").textContent =
        `${d.archivo} · ${pct.toFixed(1)}% · ${tamano(d.hecho)} de ${tamano(d.total)}` +
        (d.velocidad ? ` · ${tamano(d.velocidad)}/s · faltan ${reloj(restante)}` : "");
    }

    if (!d.activa) {
      fuente.close();
      descargaEnCurso(false);
      if (d.error) {
        $("texto-modelo").textContent = "✕ " + d.error;
      } else if (d.terminada) {
        $("texto-modelo").textContent = "✓ Modelos descargados y verificados.";
        $("barra-modelo").style.width = "100%";
        await cargarEstado();
      }
    }
  };

  fuente.onerror = () => { fuente.close(); descargaEnCurso(false); };
}

/* ======================= Voces ======================= */
// Un único reproductor para toda la biblioteca: así el botón puede parar lo que
// suena y no se solapan varias voces al pulsar seguido.
let audioVoz = null;
let vozSonando = null;

function pararVoz() {
  if (audioVoz) {
    audioVoz.pause();
    audioVoz.currentTime = 0;
    audioVoz = null;
  }
  vozSonando = null;
  document.querySelectorAll(".voz .escuchar").forEach((b) => {
    b.textContent = "▶";
    b.title = "Escuchar";
    b.classList.remove("sonando");
  });
}

function alternarVoz(id, boton) {
  const eraLaMisma = vozSonando === id;
  pararVoz();
  if (eraLaMisma) return;            // segundo clic sobre la que suena: parar

  audioVoz = new Audio(`/api/voces/${id}/audio`);
  vozSonando = id;
  boton.textContent = "⏹";
  boton.title = "Parar";
  boton.classList.add("sonando");
  audioVoz.addEventListener("ended", pararVoz);
  audioVoz.addEventListener("error", pararVoz);
  audioVoz.play().catch(pararVoz);
}

async function cargarVoces() {
  const voces = await api("/api/voces");
  estado.voces = voces;
  App.alCambiarVoces.forEach((f) => { try { f(voces); } catch { /* ignora */ } });
  const cont = $("lista-voces");

  if (!voces.length) {
    if (vozSonando) pararVoz();
    cont.innerHTML = `<div class="vacio">Aún no hay voces guardadas.<br>
      Graba o sube un audio para clonar una voz.</div>`;
    return;
  }

  cont.innerHTML = voces.map((v) => `
    <div class="voz ${estado.vozSeleccionada === v.id ? "sel" : ""}" data-id="${v.id}">
      <div class="voz-datos">
        <div class="voz-nombre">${escapar(v.nombre)}</div>
        <div class="voz-meta">${v.duracion} s${v.transcripcion ? " · " + escapar(v.transcripcion) : ""}</div>
      </div>
      <button class="icono-btn escuchar" data-id="${v.id}" title="Escuchar">▶</button>
      <button class="icono-btn borrar" data-id="${v.id}" title="Eliminar">✕</button>
    </div>`).join("");

  cont.querySelectorAll(".voz").forEach((el) => {
    el.addEventListener("click", (ev) => {
      if (ev.target.closest(".icono-btn")) return;
      const id = el.dataset.id;
      estado.vozSeleccionada = estado.vozSeleccionada === id ? null : id;
      cargarVoces();
    });
  });

  cont.querySelectorAll(".escuchar").forEach((b) => {
    b.addEventListener("click", () => alternarVoz(b.dataset.id, b));
  });

  cont.querySelectorAll(".borrar").forEach((b) => {
    b.addEventListener("click", async () => {
      if (!confirm("¿Eliminar esta voz de la biblioteca?")) return;
      if (vozSonando === b.dataset.id) pararVoz();
      await api(`/api/voces/${b.dataset.id}`, { method: "DELETE" });
      if (estado.vozSeleccionada === b.dataset.id) estado.vozSeleccionada = null;
      cargarVoces();
    });
  });

  // La lista se redibuja al seleccionar o añadir voces: devolver el botón de la
  // que siga sonando a su estado de "parar".
  if (vozSonando) {
    const boton = cont.querySelector(`.escuchar[data-id="${vozSonando}"]`);
    if (boton) {
      boton.textContent = "⏹";
      boton.title = "Parar";
      boton.classList.add("sonando");
    } else {
      pararVoz();                    // esa voz ya no está en la lista
    }
  }
}

async function subirVoz(archivo, nombre, transcripcion) {
  const datos = new FormData();
  datos.append("audio", archivo);
  datos.append("nombre", nombre);
  datos.append("transcripcion", transcripcion || "");
  const voz = await api("/api/voces", { method: "POST", body: datos });
  estado.vozSeleccionada = voz.id;
  await cargarVoces();
  return voz;
}

/* ======================= Grabación ======================= */
let grabador = null, trozos = [], tempo = null, animacion = null, contextoAudio = null;

async function alternarGrabacion() {
  const boton = $("btn-grabar");

  if (grabador && grabador.state === "recording") {
    grabador.stop();
    return;
  }

  let flujo;
  try {
    flujo = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch {
    alert("No se pudo acceder al micrófono. Revisa los permisos del navegador.");
    return;
  }

  trozos = [];
  grabador = new MediaRecorder(flujo);
  grabador.ondataavailable = (e) => e.data.size && trozos.push(e.data);
  grabador.onstop = () => {
    flujo.getTracks().forEach((t) => t.stop());
    cancelAnimationFrame(animacion);
    clearInterval(tempo);
    if (contextoAudio) { contextoAudio.close(); contextoAudio = null; }

    estado.audioGrabado = new Blob(trozos, { type: grabador.mimeType || "audio/webm" });
    const previo = $("previo-grabacion");
    previo.src = URL.createObjectURL(estado.audioGrabado);
    previo.classList.remove("oculto");
    $("guardar-grabacion").classList.remove("oculto");
    boton.textContent = "● Grabar";
    boton.classList.remove("grabando");
  };

  grabador.start();
  boton.textContent = "■ Detener";
  boton.classList.add("grabando");
  $("previo-grabacion").classList.add("oculto");
  $("guardar-grabacion").classList.add("oculto");

  const inicio = Date.now();
  tempo = setInterval(() => {
    $("cronometro").textContent = ((Date.now() - inicio) / 1000).toFixed(1) + " s";
  }, 100);

  dibujarVumetro(flujo);
}

function dibujarVumetro(flujo) {
  contextoAudio = new AudioContext();
  const analizador = contextoAudio.createAnalyser();
  analizador.fftSize = 256;
  contextoAudio.createMediaStreamSource(flujo).connect(analizador);

  const lienzo = $("vumetro");
  const ctx = lienzo.getContext("2d");
  const datos = new Uint8Array(analizador.frequencyBinCount);

  (function pintar() {
    animacion = requestAnimationFrame(pintar);
    analizador.getByteFrequencyData(datos);
    ctx.clearRect(0, 0, lienzo.width, lienzo.height);
    const ancho = lienzo.width / datos.length;
    for (let i = 0; i < datos.length; i++) {
      const alto = (datos[i] / 255) * lienzo.height;
      ctx.fillStyle = `hsl(${215 + (datos[i] / 255) * 45}, 90%, 62%)`;
      ctx.fillRect(i * ancho, lienzo.height - alto, ancho - 0.5, alto);
    }
  })();
}

/* ======================= Texto ======================= */
function actualizarContador() {
  const texto = $("texto").value.trim();
  const porBloque = parseInt($("chars_por_bloque").value, 10) || 280;
  const bloques = texto ? Math.max(1, Math.ceil(texto.length / porBloque)) : 0;
  // ~14 caracteres por segundo de habla natural
  const segundos = Math.round(texto.length / 14);
  $("contador").textContent =
    `${texto.length} caracteres · ${bloques} bloque${bloques === 1 ? "" : "s"} · ≈${segundos} s de audio`;
}

function actualizarEtiquetasRango() {
  $("v-temp").textContent = parseFloat($("temp").value).toFixed(2);
  $("v-top_p").textContent = parseFloat($("top_p").value).toFixed(2);
}

function ajustesActuales() {
  const cfg = {
    idioma: $("idioma").value,
    dispositivo: $("dispositivo").value,
  };
  for (const campo of CAMPOS_AJUSTES) cfg[campo] = parseFloat($(campo).value);
  return cfg;
}

/* ======================= Generación ======================= */
async function generar() {
  const texto = $("texto").value.trim();
  if (!texto) { alert("Escribe el texto que quieres sintetizar."); return; }

  const boton = $("btn-generar");
  boton.disabled = true;
  boton.textContent = "Generando…";
  $("btn-cancelar").classList.remove("oculto");
  $("resultado").classList.add("oculto");
  $("progreso").classList.remove("oculto");
  $("caja-consola").classList.remove("oculto");
  $("consola").textContent = "";
  $("barra-relleno").style.width = "0%";
  $("texto-progreso").textContent = "Cargando el modelo…";

  let tarea;
  try {
    tarea = await api("/api/generar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        texto,
        voz: estado.vozSeleccionada,
        ...ajustesActuales(),
      }),
    });
  } catch (err) {
    terminarGeneracion();
    $("texto-progreso").textContent = "✕ " + err.message;
    return;
  }

  estado.tareaActual = tarea.id;
  $("texto-progreso").textContent =
    `Sintetizando ${tarea.bloques} bloque(s) en ${tarea.dispositivo}…`;

  const fuente = new EventSource(`/api/tarea/${tarea.id}/eventos`);
  estado.fuenteEventos = fuente;
  let bloqueActual = 0;

  fuente.onmessage = (ev) => {
    const dato = JSON.parse(ev.data);

    if (dato.tipo === "log") {
      const consola = $("consola");
      consola.textContent += dato.linea + "\n";
      consola.scrollTop = consola.scrollHeight;

    } else if (dato.tipo === "bloque") {
      bloqueActual = dato.indice;
      $("barra-relleno").style.width = `${((dato.indice - 1) / dato.total) * 100}%`;
      $("texto-progreso").textContent =
        `Bloque ${dato.indice} de ${dato.total} · ${recortar(dato.texto, 70)}`;

    } else if (dato.tipo === "fin") {
      $("barra-relleno").style.width = "100%";
      $("texto-progreso").textContent =
        `✓ Listo en ${dato.segundos} s · ${dato.duracion} s de audio`;
      const url = `/api/salidas/${dato.archivo}`;
      $("reproductor").src = url;
      $("descargar").href = url;
      $("descargar").setAttribute("download", dato.archivo);
      $("info-resultado").textContent =
        `${dato.archivo} · ${dato.duracion} s · generado en ${dato.segundos} s`;
      $("resultado").classList.remove("oculto");
      $("reproductor").play().catch(() => { /* autoplay bloqueado */ });
      cerrarFlujo();
      cargarHistorial();

    } else if (dato.tipo === "error") {
      $("texto-progreso").textContent = "✕ Error: " + dato.mensaje;
      $("caja-consola").open = true;
      cerrarFlujo();

    } else if (dato.tipo === "cancelada") {
      $("texto-progreso").textContent = "Generación cancelada.";
      cerrarFlujo();
    }
  };

  fuente.onerror = () => {
    if (estado.fuenteEventos) {
      $("texto-progreso").textContent =
        `Se perdió la conexión con el servidor (bloque ${bloqueActual}).`;
      cerrarFlujo();
    }
  };
}

function cerrarFlujo() {
  if (estado.fuenteEventos) {
    estado.fuenteEventos.close();
    estado.fuenteEventos = null;
  }
  terminarGeneracion();
}

function terminarGeneracion() {
  const boton = $("btn-generar");
  boton.disabled = false;
  boton.textContent = "Generar audio";
  $("btn-cancelar").classList.add("oculto");
  estado.tareaActual = null;
  App.alTerminarGeneracion.forEach((f) => { try { f(); } catch { /* ignora */ } });
}

/* ======================= Historial ======================= */
async function cargarHistorial() {
  const salidas = await api("/api/salidas");
  const cont = $("historial");

  if (!salidas.length) {
    cont.innerHTML = `<div class="vacio">Todavía no has generado ningún audio.</div>`;
    return;
  }

  cont.innerHTML = salidas.map((s) => `
    <div class="item-historial">
      <audio controls preload="none" src="/api/salidas/${s.archivo}"></audio>
      <div class="item-cabecera">
        <span>${s.fecha.replace("T", " ")} · ${s.duracion} s</span>
        <span>
          <a class="icono-btn" href="/api/salidas/${s.archivo}" download title="Descargar">⭳</a>
          <button class="icono-btn borrar" data-archivo="${s.archivo}" title="Eliminar">✕</button>
        </span>
      </div>
    </div>`).join("");

  cont.querySelectorAll(".borrar").forEach((b) => {
    b.addEventListener("click", async () => {
      await api(`/api/salidas/${b.dataset.archivo}`, { method: "DELETE" });
      cargarHistorial();
    });
  });
}

/* ======================= Utilidades ======================= */
const escapar = (t) => String(t).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const recortar = (t, n) => (t.length > n ? t.slice(0, n) + "…" : t);

function tamano(n) {
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${i === 0 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
}

function reloj(segundos) {
  if (!isFinite(segundos) || segundos <= 0) return "—";
  const m = Math.floor(segundos / 60), s = Math.round(segundos % 60);
  return m ? `${m} min ${s.toString().padStart(2, "0")} s` : `${s} s`;
}

/* ======================= Arranque ======================= */
function conectarEventos() {
  // Pestañas
  document.querySelectorAll(".pestana").forEach((p) => {
    p.addEventListener("click", () => {
      document.querySelectorAll(".pestana").forEach((o) => o.classList.remove("activa"));
      document.querySelectorAll(".panel").forEach((o) => o.classList.add("oculto"));
      p.classList.add("activa");
      $(p.dataset.panel).classList.remove("oculto");
    });
  });

  // Grabación
  $("btn-grabar").addEventListener("click", alternarGrabacion);
  $("btn-guardar-grabacion").addEventListener("click", async () => {
    const nombre = $("nombre-grabacion").value.trim();
    if (!nombre) { alert("Ponle un nombre a la voz."); return; }
    if (!estado.audioGrabado) return;
    const boton = $("btn-guardar-grabacion");
    boton.disabled = true;
    try {
      await subirVoz(new File([estado.audioGrabado], "grabacion.webm"),
                     nombre, $("trans-grabacion").value);
      $("guardar-grabacion").classList.add("oculto");
      $("previo-grabacion").classList.add("oculto");
      $("nombre-grabacion").value = $("trans-grabacion").value = "";
      $("cronometro").textContent = "0.0 s";
      document.querySelector('[data-panel="p-biblioteca"]').click();
    } catch (err) {
      alert("No se pudo guardar la voz: " + err.message);
    } finally {
      boton.disabled = false;
    }
  });

  // Subida de archivo
  const zona = $("zona-archivo"), entrada = $("archivo-voz");
  entrada.addEventListener("change", () => {
    if (!entrada.files.length) return;
    $("texto-archivo").textContent = entrada.files[0].name;
    $("btn-subir-voz").disabled = false;
    if (!$("nombre-archivo-voz").value) {
      $("nombre-archivo-voz").value = entrada.files[0].name.replace(/\.[^.]+$/, "");
    }
  });

  ["dragover", "dragleave", "drop"].forEach((evento) => {
    zona.addEventListener(evento, (e) => {
      e.preventDefault();
      zona.classList.toggle("encima", evento === "dragover");
      if (evento === "drop" && e.dataTransfer.files.length) {
        entrada.files = e.dataTransfer.files;
        entrada.dispatchEvent(new Event("change"));
      }
    });
  });

  $("btn-subir-voz").addEventListener("click", async () => {
    const nombre = $("nombre-archivo-voz").value.trim();
    if (!nombre) { alert("Ponle un nombre a la voz."); return; }
    const boton = $("btn-subir-voz");
    boton.disabled = true;
    boton.textContent = "Procesando…";
    try {
      await subirVoz(entrada.files[0], nombre, $("trans-archivo-voz").value);
      entrada.value = "";
      $("texto-archivo").textContent = "Arrastra un audio aquí o haz clic para elegirlo";
      $("nombre-archivo-voz").value = $("trans-archivo-voz").value = "";
      document.querySelector('[data-panel="p-biblioteca"]').click();
    } catch (err) {
      alert("No se pudo añadir la voz: " + err.message);
    } finally {
      boton.textContent = "Añadir a la biblioteca";
      boton.disabled = !entrada.files.length;
    }
  });

  // Texto y ajustes
  $("texto").addEventListener("input", actualizarContador);
  $("chars_por_bloque").addEventListener("input", actualizarContador);
  $("temp").addEventListener("input", actualizarEtiquetasRango);
  $("top_p").addEventListener("input", actualizarEtiquetasRango);

  $("btn-ejemplo").addEventListener("click", () => {
    $("texto").value =
      "Hola, esta es una prueba de clonación de voz ejecutándose por completo en " +
      "mi propio ordenador. El modelo Qwen3 TTS funciona tanto en procesador como " +
      "en tarjeta gráfica, sin enviar nada a internet.";
    actualizarContador();
  });

  $("btn-guardar-ajustes").addEventListener("click", async () => {
    await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ajustesActuales()),
    });
    const boton = $("btn-guardar-ajustes");
    boton.textContent = "✓ Guardado";
    setTimeout(() => { boton.textContent = "Guardar como predeterminados"; }, 1800);
  });

  // Descarga del modelo
  $("btn-descargar-modelo").addEventListener("click", descargarModelo);
  $("btn-cancelar-descarga").addEventListener("click", async () => {
    await api("/api/modelo/cancelar", { method: "POST" });
  });

  // Generación
  $("btn-generar").addEventListener("click", generar);
  $("btn-cancelar").addEventListener("click", async () => {
    if (estado.tareaActual) {
      await api(`/api/tarea/${estado.tareaActual}/cancelar`, { method: "POST" });
    }
  });

  // Ctrl+Enter genera
  $("texto").addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.key === "Enter") generar();
  });
}

Object.assign(App, { escapar, recortar, tamano, reloj, cargarHistorial, cargarVoces,
                     generar, actualizarContador });

(async function iniciar() {
  conectarEventos();
  actualizarContador();
  try {
    await cargarEstado();
  } catch (err) {
    $("aviso").classList.remove("oculto");
    $("aviso").textContent = "No se pudo contactar con el servidor: " + err.message;
  }
  await cargarVoces();
  await cargarHistorial();
})();
