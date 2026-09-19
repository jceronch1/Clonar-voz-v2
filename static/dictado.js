"use strict";
/* Dictado por micrófono con detección de pausas: hablas, callas, y genera solo. */

(function () {
  const $ = (id) => document.getElementById(id);
  const { api } = window.App;

  const D = {
    activo: false,
    escuchando: false,          // el bucle de detección está midiendo
    flujo: null,                // MediaStream del micrófono
    grabador: null,             // MediaRecorder de la frase en curso
    ctx: null,
    analizador: null,
    datos: null,
    trozos: [],
    anim: null,
    // Detección de voz
    ruidoBase: 0.004,
    muestrasRuido: [],
    calibrando: true,
    haHablado: false,
    inicioHabla: 0,
    ultimoSonido: 0,
    nivel: 0,
    // Ajustes (se rellenan desde el servidor)
    cfg: {
      dictado_silencio_ms: 1200,
      dictado_sensibilidad: 2.5,
      dictado_autogenerar: true,
      dictado_continuo: true,
    },
  };

  const MIN_FRASE_MS = 350;     // por debajo de esto es un ruido, no una frase
  const MAX_FRASE_MS = 30000;   // tope de seguridad: corta aunque no calles
  const CALIBRACION_MS = 700;
  const PISO_ABSOLUTO = 0.004;  // umbral mínimo, por si el micro es muy silencioso
  const ARRANQUE_VOZ_MS = 90;   // hay que superar el umbral este rato para contar
  const HISTERESIS = 0.55;      // el umbral para seguir hablando es más bajo

  /* ==================== Ajustes ==================== */
  async function cargarEstadoDictado() {
    let e;
    try {
      e = await api("/api/dictado/estado");
    } catch {
      return;
    }

    $("stt_modelo").innerHTML = Object.entries(e.modelos)
      .map(([clave, etiqueta]) =>
        `<option value="${clave}">${etiqueta}</option>`).join("");
    Object.assign(D.cfg, e.config);

    $("stt_modelo").value = e.config.stt_modelo || "small";
    $("stt_dispositivo").value = e.config.stt_dispositivo || "auto";
    $("stt_comandos").checked = e.config.stt_comandos !== false;
    $("stt_pulir_llm").checked = e.config.stt_pulir_llm === true;
    $("dictado_autogenerar").checked = e.config.dictado_autogenerar !== false;
    $("dictado_continuo").checked = e.config.dictado_continuo !== false;
    $("dictado_silencio_ms").value = e.config.dictado_silencio_ms ?? 1200;
    $("dictado_sensibilidad").value = e.config.dictado_sensibilidad ?? 2.5;
    etiquetasAjustes();

    if (!e.disponible) {
      $("btn-dictado").disabled = true;
      poner("Falta faster-whisper (pip install faster-whisper)");
    } else if (e.cargado) {
      poner(`Modelo ${e.modelo} listo en ${e.dispositivo}`);
    } else {
      $("stt-info").textContent = e.cuda
        ? "Whisper en local · GPU CUDA disponible"
        : "Whisper en local · se ejecutará en CPU";
    }
  }

  function etiquetasAjustes() {
    $("v-silencio").textContent = $("dictado_silencio_ms").value;
    $("v-sensibilidad").textContent = Number($("dictado_sensibilidad").value).toFixed(1);
  }

  const guardar = (datos) => {
    Object.assign(D.cfg, datos);
    return api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(datos),
    }).catch(() => {});
  };

  const poner = (txt) => { $("dictado-estado").textContent = txt; };

  function pintarBoton(modo) {
    const b = $("btn-dictado");
    b.classList.toggle("escuchando", modo === "escuchando");
    b.classList.toggle("ocupado", modo === "ocupado");
    b.textContent = modo === "parado" ? "🎙️ Dictar" : "⏹ Parar";
  }

  /* ==================== Medidor de nivel ==================== */
  function pintarNivel(umbral) {
    const lienzo = $("nivel-voz");
    const ctx = lienzo.getContext("2d");
    const { width: an, height: al } = lienzo;
    ctx.clearRect(0, 0, an, al);

    // Escala logarítmica: la voz se mueve en valores pequeños
    const escala = (v) => Math.min(1, Math.sqrt(v / 0.25));
    const ancho = escala(D.nivel) * an;
    const activo = D.nivel > umbral;

    const grad = ctx.createLinearGradient(0, 0, an, 0);
    grad.addColorStop(0, activo ? "#3fb950" : "#4f8cff");
    grad.addColorStop(1, activo ? "#7ee787" : "#7c5cff");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, ancho, al);

    ctx.fillStyle = "rgba(255,255,255,.45)";          // marca del umbral
    ctx.fillRect(escala(umbral) * an, 0, 2, al);
  }

  /* ==================== Detección de voz ==================== */
  function bucle() {
    if (!D.activo) return;
    D.anim = requestAnimationFrame(bucle);

    D.analizador.getByteTimeDomainData(D.datos);
    let suma = 0;
    for (let i = 0; i < D.datos.length; i++) {
      const v = (D.datos[i] - 128) / 128;
      suma += v * v;
    }
    D.nivel = Math.sqrt(suma / D.datos.length);

    const ahora = performance.now();

    if (D.calibrando) {
      D.muestrasRuido.push(D.nivel);
      if (ahora - D.inicioCalibracion > CALIBRACION_MS) {
        const ordenadas = [...D.muestrasRuido].sort((a, b) => a - b);
        D.ruidoBase = Math.max(ordenadas[Math.floor(ordenadas.length / 2)] || 0, 0.002);
        D.calibrando = false;
        poner("Escuchando… habla cuando quieras");
      }
      pintarNivel(PISO_ABSOLUTO);
      return;
    }

    // Umbral relativo al ruido de fondo, con histéresis: cuesta más empezar a
    // hablar que seguir hablando, así no se corta entre palabra y palabra.
    const umbralAlto = Math.max(D.ruidoBase * D.cfg.dictado_sensibilidad, PISO_ABSOLUTO);
    const umbralBajo = umbralAlto * HISTERESIS;
    pintarNivel(umbralAlto);

    if (!D.escuchando) return;

    const umbral = D.haHablado ? umbralBajo : umbralAlto;

    if (D.nivel > umbral) {
      if (!D.haHablado) {
        // Exigir un ratito por encima del umbral: un golpe seco no es voz
        D.arranque = D.arranque || ahora;
        if (ahora - D.arranque < ARRANQUE_VOZ_MS) return;
        D.haHablado = true;
        D.inicioHabla = D.arranque;
        poner("Te escucho…");
      }
      D.ultimoSonido = ahora;
      return;
    }

    D.arranque = 0;

    // El ruido de fondo se reestima en los silencios: el micrófono cambia de
    // ganancia solo y un umbral fijo acaba dejando de valer.
    if (!D.haHablado) {
      D.ruidoBase = Math.max(D.ruidoBase * 0.99 + D.nivel * 0.01, 0.002);
      return;
    }

    // Silencio tras haber hablado: ¿ha durado lo bastante para cerrar la frase?
    const habla = D.ultimoSonido - D.inicioHabla;
    if (ahora - D.ultimoSonido > D.cfg.dictado_silencio_ms && habla > MIN_FRASE_MS) {
      cerrarFrase();
    } else if (habla > MAX_FRASE_MS) {
      cerrarFrase();
    }
  }

  /* ==================== Grabación por frases ==================== */
  function nuevoGrabador() {
    D.trozos = [];
    D.grabador = new MediaRecorder(D.flujo);
    D.grabador.ondataavailable = (e) => e.data.size && D.trozos.push(e.data);
    D.grabador.onstop = () => {
      const blob = new Blob(D.trozos, { type: D.grabador.mimeType || "audio/webm" });
      D.trozos = [];
      if (D.pendiente) { D.pendiente = false; procesar(blob); }
    };
    D.grabador.start();
  }

  function cerrarFrase() {
    D.escuchando = false;
    D.haHablado = false;
    D.pendiente = true;
    pintarBoton("ocupado");
    poner("Transcribiendo…");
    if (D.grabador && D.grabador.state === "recording") D.grabador.stop();
  }

  async function procesar(blob) {
    console.debug("[dictado] blob", blob.size, "bytes", blob.type);
    if (blob.size < 2000) {            // casi nada: vuelve a escuchar
      poner("Audio demasiado corto; inténtalo otra vez");
      return reanudar();
    }
    try {
      const datos = new FormData();
      datos.append("audio", new File([blob], "dictado.webm"));
      const r = await fetch("/api/dictado/transcribir", { method: "POST", body: datos });
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try { msg = (await r.json()).detail || msg; } catch { /* sin JSON */ }
        throw new Error(msg);
      }
      const t = await r.json();

      if (!t.texto) {
        poner("No se entendió nada; sigue hablando");
        return reanudar();
      }

      const caja = $("texto");
      // Con autogenerar, cada frase es su propio audio: se reemplaza.
      // Sin autogenerar, se va dictando un texto largo: se acumula.
      caja.value = D.cfg.dictado_autogenerar
        ? t.texto
        : (caja.value.trim() ? caja.value.trim() + " " + t.texto : t.texto);
      window.App.actualizarContador();
      poner(`"${t.texto.slice(0, 46)}${t.texto.length > 46 ? "…" : ""}" · ${t.segundos}s`);

      if (D.cfg.dictado_autogenerar) {
        D.esperandoGeneracion = true;
        pintarBoton("ocupado");
        window.App.generar();
      } else {
        reanudar();
      }
    } catch (err) {
      poner("✕ " + err.message);
      reanudar();
    }
  }

  /** Vuelve a escuchar tras una frase, si el modo continuo sigue activo. */
  function reanudar() {
    if (!D.activo) return;
    if (!D.cfg.dictado_continuo) { parar(); return; }
    D.haHablado = false;
    D.pendiente = false;
    D.escuchando = true;
    pintarBoton("escuchando");
    poner("Escuchando… habla cuando quieras");
    nuevoGrabador();
  }

  /* ==================== Arranque y parada ==================== */
  async function arrancar() {
    try {
      D.flujo = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,     // evita que capte el audio que genera
          noiseSuppression: true,
          // El control de ganancia sube el volumen en los silencios y hace
          // bailar el umbral: mejor nivel estable para detectar las pausas.
          autoGainControl: false,
        },
      });
    } catch {
      poner("✕ Sin acceso al micrófono");
      return;
    }

    D.activo = true;
    D.escuchando = true;
    D.calibrando = true;
    D.muestrasRuido = [];
    D.inicioCalibracion = performance.now();
    D.haHablado = false;
    D.arranque = 0;

    D.ctx = new AudioContext();
    D.analizador = D.ctx.createAnalyser();
    D.analizador.fftSize = 1024;
    D.datos = new Uint8Array(D.analizador.fftSize);
    D.ctx.createMediaStreamSource(D.flujo).connect(D.analizador);

    nuevoGrabador();
    pintarBoton("escuchando");
    poner("Calibrando el ruido de fondo…");
    bucle();

    // Cargar el modelo en segundo plano: así la primera frase no espera
    api("/api/dictado/cargar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ modelo: $("stt_modelo").value,
                             dispositivo: $("stt_dispositivo").value }),
    }).catch((err) => poner("✕ " + err.message));
  }

  function parar() {
    D.activo = D.escuchando = D.pendiente = false;
    cancelAnimationFrame(D.anim);
    if (D.grabador && D.grabador.state === "recording") D.grabador.stop();
    if (D.flujo) D.flujo.getTracks().forEach((t) => t.stop());
    if (D.ctx) { D.ctx.close(); D.ctx = null; }
    D.flujo = D.grabador = null;
    pintarBoton("parado");
    poner("Pulsa para hablar");
    const lienzo = $("nivel-voz");
    lienzo.getContext("2d").clearRect(0, 0, lienzo.width, lienzo.height);
  }

  /* ==================== Enganches ==================== */
  function conectar() {
    $("btn-dictado").addEventListener("click", () => (D.activo ? parar() : arrancar()));
    $("btn-dictado-ajustes").addEventListener("click", () =>
      $("dictado-ajustes").classList.toggle("oculto"));

    $("dictado_silencio_ms").addEventListener("input", etiquetasAjustes);
    $("dictado_sensibilidad").addEventListener("input", etiquetasAjustes);
    $("dictado_silencio_ms").addEventListener("change", (e) =>
      guardar({ dictado_silencio_ms: parseInt(e.target.value, 10) }));
    $("dictado_sensibilidad").addEventListener("change", (e) =>
      guardar({ dictado_sensibilidad: parseFloat(e.target.value) }));
    $("dictado_autogenerar").addEventListener("change", (e) =>
      guardar({ dictado_autogenerar: e.target.checked }));
    $("dictado_continuo").addEventListener("change", (e) =>
      guardar({ dictado_continuo: e.target.checked }));
    $("stt_comandos").addEventListener("change", (e) =>
      guardar({ stt_comandos: e.target.checked }));
    $("stt_pulir_llm").addEventListener("change", (e) =>
      guardar({ stt_pulir_llm: e.target.checked }));
    $("stt_modelo").addEventListener("change", (e) =>
      guardar({ stt_modelo: e.target.value }));
    $("stt_dispositivo").addEventListener("change", (e) =>
      guardar({ stt_dispositivo: e.target.value }));

    $("btn-stt-parar").addEventListener("click", async () => {
      await api("/api/dictado/parar", { method: "POST" });
      $("stt-info").textContent = "Modelo descargado de la memoria";
    });

    // Cuando acaba una síntesis lanzada por dictado, esperar a que suene
    // y volver a escuchar. Si no se espera, el micro capta el audio generado.
    window.App.alTerminarGeneracion.push(() => {
      if (!D.esperandoGeneracion) return;
      D.esperandoGeneracion = false;
      const audio = $("reproductor");
      if (audio && !audio.paused && !audio.ended) {
        audio.addEventListener("ended", () => setTimeout(reanudar, 250), { once: true });
      } else {
        setTimeout(reanudar, 400);
      }
    });

    // Al salir de la pestaña de voz simple, dejar de escuchar
    document.querySelectorAll(".modo").forEach((b) => {
      b.addEventListener("click", () => {
        if (D.activo && b.dataset.vista !== "vista-simple") parar();
      });
    });
    window.addEventListener("beforeunload", () => { if (D.activo) parar(); });
  }

  conectar();
  cargarEstadoDictado();
})();
