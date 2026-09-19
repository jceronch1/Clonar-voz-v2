"use strict";
/* Guion multivoz: modelo de texto local + reparto de voces + síntesis completa. */

(function () {
  const $ = (id) => document.getElementById(id);
  const { api, escapar, recortar } = window.App;

  const G = {
    segmentos: [],          // [{personaje, voz, texto, pausa_ms}]
    voces: [],
    dispositivos: [],
    idGuion: null,
    tareaEscritura: null,
    tareaAudio: null,
    fuente: null,
    tiempos: [],
  };

  // Un color estable por personaje, para reconocerlo de un vistazo
  const PALETA = ["#4f8cff", "#7c5cff", "#3fb950", "#d29922", "#f85149",
                  "#2dd4bf", "#f472b6", "#a78bfa", "#fb923c", "#38bdf8"];
  const colorDe = (personaje) => {
    const orden = personajes();
    const i = orden.indexOf(personaje);
    return PALETA[(i < 0 ? 0 : i) % PALETA.length];
  };

  const personajes = () => {
    const vistos = [];
    for (const s of G.segmentos) if (!vistos.includes(s.personaje)) vistos.push(s.personaje);
    return vistos;
  };

  /* ==================== Modelo de texto ==================== */
  async function cargarCatalogoLLM() {
    let datos;
    try {
      datos = await api("/api/llm/modelos");
    } catch {
      return;
    }

    const opciones = datos.modelos.map((m) => {
      const gb = (m.bytes / 2 ** 30).toFixed(2);
      let marca = "";
      if (m.es_embedding) marca = " · no sirve para escribir";
      else if (m.compatible === false) marca = " · ✕ no carga";
      else if (m.compatible === true) marca = " · ✓ probado";
      return `<option value="${m.id}" ${m.es_embedding ? "disabled" : ""}>` +
             `${escapar(m.nombre)} — ${gb} GB${marca}</option>`;
    });

    $("llm-modelo").innerHTML = opciones.length
      ? opciones.join("")
      : `<option value="">No se encontró ningún modelo local</option>`;

    const guardado = window.App.estado.sistema?.config?.llm_modelo;
    if (guardado && datos.modelos.some((m) => m.id === guardado)) {
      $("llm-modelo").value = guardado;
    }
  }

  function pintarDispositivosLLM(s) {
    const opciones = [`<option value="auto">Automático (mejor GPU)</option>`];
    for (const d of s.dispositivos) {
      opciones.push(`<option value="${d.id}">GPU · ${escapar(d.nombre)}</option>`);
    }
    opciones.push(`<option value="cpu">Solo CPU (${s.cpus} núcleos)</option>`);
    $("llm-dispositivo").innerHTML = opciones.join("");
    $("llm-dispositivo").value = s.config.llm_dispositivo || "auto";
    $("llm-contexto").value = s.config.llm_contexto || 8192;
    $("llm_temp").value = s.config.llm_temp ?? 0.85;
    $("v-llm_temp").textContent = Number($("llm_temp").value).toFixed(2);
    $("liberar_llm").value = s.config.liberar_llm || "auto";
  }

  function pintarEstadoLLM(e) {
    const punto = $("llm-estado").querySelector(".punto");
    const texto = e.cargado ? `${e.nombre} en ${e.dispositivo}`
                : e.cargando ? "cargando…"
                : e.error ? "error" : "sin cargar";
    punto.className = "punto " + (e.cargado ? "ok" : e.error ? "mal" : "");
    $("llm-estado").lastChild.textContent = " " + texto;
    $("btn-llm-parar").classList.toggle("oculto", !e.cargado);
    $("btn-escribir").disabled = !e.cargado;

    if (e.registro?.length) {
      $("caja-llm-log").classList.remove("oculto");
      $("llm-log").textContent = e.registro.join("\n");
      $("llm-log").scrollTop = $("llm-log").scrollHeight;
    }
  }

  async function cargarModelo() {
    const boton = $("btn-llm-cargar");
    boton.disabled = true;
    boton.textContent = "Cargando…";
    pintarEstadoLLM({ cargando: true });

    const sonda = setInterval(async () => {
      try { pintarEstadoLLM(await api("/api/llm/estado")); } catch { /* aún no */ }
    }, 1500);

    try {
      pintarEstadoLLM(await api("/api/llm/cargar", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          modelo: $("llm-modelo").value,
          dispositivo: $("llm-dispositivo").value,
          contexto: parseInt($("llm-contexto").value, 10) || 8192,
        }),
      }));
    } catch (err) {
      pintarEstadoLLM({ error: err.message });
      $("caja-llm-log").open = true;
      alert("No se pudo cargar el modelo:\n\n" + err.message);
    } finally {
      clearInterval(sonda);
      boton.disabled = false;
      boton.textContent = "Cargar modelo";
    }
  }

  async function probarModelo() {
    const boton = $("btn-llm-probar");
    boton.disabled = true;
    boton.textContent = "Comprobando…";
    try {
      const r = await api("/api/llm/comprobar", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ modelo: $("llm-modelo").value }),
      });
      alert(r.ok ? "✓ Este modelo carga correctamente en llama.cpp."
                 : "✕ Este modelo no carga:\n\n" + r.error);
      await cargarCatalogoLLM();
    } catch (err) {
      alert("No se pudo comprobar: " + err.message);
    } finally {
      boton.disabled = false;
      boton.textContent = "Comprobar compatibilidad";
    }
  }

  /* ==================== Escritura del guion ==================== */
  async function escribirGuion() {
    const idea = $("idea").value.trim();
    if (!idea) { alert("Cuéntale al modelo qué historia quieres."); return; }

    $("btn-escribir").disabled = true;
    $("btn-parar-escritura").classList.remove("oculto");
    $("borrador-caja").classList.remove("oculto");
    $("borrador").value = "";
    $("borrador-info").textContent = "Escribiendo…";

    let tarea;
    try {
      tarea = await api("/api/llm/guion", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          idea,
          personajes: parseInt($("n-personajes").value, 10) || 0,
          palabras: parseInt($("n-palabras").value, 10) || 0,
          idioma: $("idioma").value,
        }),
      });
    } catch (err) {
      terminarEscritura();
      $("borrador-info").textContent = "✕ " + err.message;
      return;
    }

    G.tareaEscritura = tarea.id;
    const fuente = new EventSource(`/api/tarea/${tarea.id}/eventos`);
    const caja = $("borrador");
    const inicio = Date.now();

    fuente.onmessage = (ev) => {
      const d = JSON.parse(ev.data);
      if (d.tipo === "texto") {
        caja.value += d.trozo;
        caja.scrollTop = caja.scrollHeight;
        $("borrador-info").textContent =
          `Escribiendo… ${caja.value.length} caracteres · ${((Date.now() - inicio) / 1000).toFixed(0)} s`;
      } else if (d.tipo === "fin") {
        caja.value = d.texto;
        $("borrador-info").textContent =
          `✓ ${d.segmentos.length} intervenciones · ${d.personajes.length} personajes`;
        fuente.close();
        terminarEscritura();
        if (d.segmentos.length <= 1) {
          $("borrador-info").textContent +=
            " — el modelo no respetó el formato [PERSONAJE]. Prueba con uno más grande, o edita el texto y pulsa «Convertir en guion».";
        } else {
          usarSegmentos(d.segmentos);
        }
      } else if (d.tipo === "error") {
        $("borrador-info").textContent = "✕ " + d.mensaje;
        fuente.close();
        terminarEscritura();
      } else if (d.tipo === "cancelada") {
        $("borrador-info").textContent = "Escritura detenida.";
        fuente.close();
        terminarEscritura();
      }
    };
    fuente.onerror = () => { fuente.close(); terminarEscritura(); };
  }

  function terminarEscritura() {
    $("btn-escribir").disabled = false;
    $("btn-parar-escritura").classList.add("oculto");
    G.tareaEscritura = null;
  }

  async function convertirBorrador() {
    const texto = $("borrador").value.trim();
    if (!texto) return;
    const r = await api("/api/llm/analizar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texto }),
    });
    if (!r.segmentos.length) { alert("No se pudo sacar ninguna intervención del texto."); return; }
    usarSegmentos(r.segmentos);
  }

  /* ==================== Segmentos y reparto ==================== */
  function usarSegmentos(segmentos) {
    G.segmentos = segmentos.map((s) => ({
      personaje: s.personaje || "NARRADOR",
      texto: s.texto || "",
      voz: s.voz || null,
      pausa_ms: s.pausa_ms ?? 420,
    }));
    repartirAuto(false);
    pintarTodo();
    $("caja-guion").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function repartirAuto(forzar = true) {
    const nombres = personajes();
    nombres.forEach((p, i) => {
      const yaTiene = G.segmentos.find((s) => s.personaje === p && s.voz);
      if (!forzar && yaTiene) return;
      const voz = G.voces[i % Math.max(G.voces.length, 1)];
      if (voz) G.segmentos.forEach((s) => { if (s.personaje === p) s.voz = voz.id; });
    });
  }

  function opcionesVoz(seleccionada) {
    return [`<option value="">— voz por defecto —</option>`]
      .concat(G.voces.map((v) =>
        `<option value="${v.id}" ${v.id === seleccionada ? "selected" : ""}>${escapar(v.nombre)}</option>`))
      .join("");
  }

  function pintarReparto() {
    const nombres = personajes();
    $("caja-reparto").classList.toggle("oculto", !nombres.length);
    $("reparto").innerHTML = nombres.map((p) => {
      const voz = G.segmentos.find((s) => s.personaje === p)?.voz || "";
      const n = G.segmentos.filter((s) => s.personaje === p).length;
      return `<div class="reparto-fila">
        <span class="chip-color" style="background:${colorDe(p)}"></span>
        <span class="reparto-nombre" title="${escapar(p)} · ${n} intervenciones">${escapar(p)}</span>
        <select data-personaje="${escapar(p)}">${opcionesVoz(voz)}</select>
      </div>`;
    }).join("");

    $("reparto").querySelectorAll("select").forEach((sel) => {
      sel.addEventListener("change", () => {
        const p = sel.dataset.personaje;
        G.segmentos.forEach((s) => { if (s.personaje === p) s.voz = sel.value || null; });
        pintarSegmentos();
      });
    });
  }

  function pintarSegmentos() {
    $("caja-guion").classList.toggle("oculto", !G.segmentos.length);
    $("segmentos").innerHTML = G.segmentos.map((s, i) => `
      <div class="segmento" data-i="${i}" style="border-left-color:${colorDe(s.personaje)}">
        <div class="seg-cabecera">
          <span class="seg-num">${i + 1}</span>
          <input class="seg-personaje" value="${escapar(s.personaje)}" list="lista-personajes" maxlength="40">
          <select class="seg-voz">${opcionesVoz(s.voz)}</select>
          <input class="seg-pausa" type="number" min="0" max="4000" step="20"
                 value="${s.pausa_ms}" title="Pausa después de esta intervención (ms)">
          <span class="seg-acciones">
            <button class="icono-btn seg-oir" title="Escuchar esta intervención">▶</button>
            <button class="icono-btn seg-subir" title="Subir">↑</button>
            <button class="icono-btn seg-bajar" title="Bajar">↓</button>
            <button class="icono-btn borrar seg-quitar" title="Eliminar">✕</button>
          </span>
        </div>
        <textarea class="seg-texto" rows="2">${escapar(s.texto)}</textarea>
      </div>`).join("") +
      `<datalist id="lista-personajes">${personajes().map((p) =>
        `<option value="${escapar(p)}">`).join("")}</datalist>`;

    $("segmentos").querySelectorAll(".segmento").forEach((el) => {
      const i = parseInt(el.dataset.i, 10);
      el.querySelector(".seg-personaje").addEventListener("change", (e) => {
        G.segmentos[i].personaje = e.target.value.trim().toUpperCase() || "NARRADOR";
        pintarTodo();
      });
      el.querySelector(".seg-voz").addEventListener("change", (e) => {
        G.segmentos[i].voz = e.target.value || null;
        pintarReparto();
      });
      el.querySelector(".seg-pausa").addEventListener("change", (e) => {
        G.segmentos[i].pausa_ms = parseInt(e.target.value, 10) || 0;
      });
      el.querySelector(".seg-texto").addEventListener("input", (e) => {
        G.segmentos[i].texto = e.target.value;
      });
      el.querySelector(".seg-oir").addEventListener("click", () => oirSegmento(i, el));
      el.querySelector(".seg-subir").addEventListener("click", () => mover(i, -1));
      el.querySelector(".seg-bajar").addEventListener("click", () => mover(i, 1));
      el.querySelector(".seg-quitar").addEventListener("click", () => {
        G.segmentos.splice(i, 1);
        pintarTodo();
      });
    });
  }

  function mover(i, delta) {
    const j = i + delta;
    if (j < 0 || j >= G.segmentos.length) return;
    [G.segmentos[i], G.segmentos[j]] = [G.segmentos[j], G.segmentos[i]];
    pintarTodo();
  }

  const pintarTodo = () => { pintarReparto(); pintarSegmentos(); };

  /* ==================== Escuchar una intervención suelta ==================== */
  async function oirSegmento(i, el) {
    const seg = G.segmentos[i];
    if (!seg.texto.trim()) return;
    const boton = el.querySelector(".seg-oir");
    boton.textContent = "…";
    el.classList.add("suena");
    try {
      const t = await api("/api/generar", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ texto: seg.texto, voz: seg.voz, idioma: $("idioma").value }),
      });
      await new Promise((listo) => {
        const f = new EventSource(`/api/tarea/${t.id}/eventos`);
        f.onmessage = (ev) => {
          const d = JSON.parse(ev.data);
          if (d.tipo === "fin") {
            new Audio(`/api/salidas/${d.archivo}`).play().catch(() => {});
            f.close(); listo();
          } else if (d.tipo === "error") {
            alert("No se pudo sintetizar: " + d.mensaje);
            f.close(); listo();
          }
        };
        f.onerror = () => { f.close(); listo(); };
      });
    } catch (err) {
      alert("No se pudo sintetizar: " + err.message);
    } finally {
      boton.textContent = "▶";
      el.classList.remove("suena");
    }
  }

  /* ==================== Generar el audio completo ==================== */
  async function generarGuion() {
    if (!G.segmentos.some((s) => s.texto.trim())) { alert("El guion está vacío."); return; }

    $("btn-generar-guion").disabled = true;
    $("btn-generar-guion").textContent = "Generando…";
    $("btn-cancelar-guion").classList.remove("oculto");
    $("resultado-guion").classList.add("oculto");
    $("progreso-guion").classList.remove("oculto");
    $("caja-consola-guion").classList.remove("oculto");
    $("consola-guion").textContent = "";
    $("barra-guion").style.width = "0%";
    $("texto-guion").textContent = "Preparando…";

    let tarea;
    try {
      tarea = await api("/api/guion/sintetizar", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          titulo: $("titulo-guion").value.trim() || "guion",
          segmentos: G.segmentos,
          idioma: $("idioma").value,
          dispositivo: $("dispositivo").value,
        }),
      });
    } catch (err) {
      finGeneracion();
      $("texto-guion").textContent = "✕ " + err.message;
      return;
    }

    G.tareaAudio = tarea.id;
    $("texto-guion").textContent = `${tarea.segmentos} intervenciones en ${tarea.dispositivo}…`;

    const fuente = new EventSource(`/api/tarea/${tarea.id}/eventos`);
    G.fuente = fuente;

    fuente.onmessage = (ev) => {
      const d = JSON.parse(ev.data);
      if (d.tipo === "log") {
        const c = $("consola-guion");
        c.textContent += d.linea + "\n";
        c.scrollTop = c.scrollHeight;
      } else if (d.tipo === "bloque") {
        $("barra-guion").style.width = `${((d.indice - 1) / d.total) * 100}%`;
        $("texto-guion").textContent =
          `${d.indice}/${d.total} · [${d.personaje}] ${recortar(d.texto, 60)}`;
      } else if (d.tipo === "fin") {
        $("barra-guion").style.width = "100%";
        $("texto-guion").textContent =
          `✓ Listo en ${d.segundos} s · ${d.duracion} s de audio`;
        mostrarResultado(d);
        fuente.close();
        finGeneracion();
        window.App.cargarHistorial();
      } else if (d.tipo === "error") {
        $("texto-guion").textContent = "✕ " + d.mensaje;
        $("caja-consola-guion").open = true;
        fuente.close();
        finGeneracion();
      } else if (d.tipo === "cancelada") {
        $("texto-guion").textContent = "Generación cancelada.";
        fuente.close();
        finGeneracion();
      }
    };
    fuente.onerror = () => { fuente.close(); finGeneracion(); };
  }

  function finGeneracion() {
    $("btn-generar-guion").disabled = false;
    $("btn-generar-guion").textContent = "🎬 Generar audio completo";
    $("btn-cancelar-guion").classList.add("oculto");
    G.tareaAudio = null;
    G.fuente = null;
  }

  function mostrarResultado(d) {
    G.tiempos = d.tiempos || [];
    const audio = $("reproductor-guion");
    audio.src = `/api/salidas/${d.archivo}`;
    $("descargar-guion").href = `/api/salidas/${d.archivo}`;
    $("descargar-guion").setAttribute("download", d.archivo);
    $("descargar-srt").href = `/api/salidas/${d.srt}`;
    $("descargar-srt").setAttribute("download", d.srt);
    $("info-guion").textContent =
      `${d.archivo} · ${d.duracion} s · ${G.tiempos.length} intervenciones`;

    $("linea-tiempo").innerHTML = G.tiempos.map((t, i) => `
      <div class="tl-item" data-i="${i}" data-inicio="${t.inicio}">
        <span class="tl-tiempo">${reloj(t.inicio)}</span>
        <span class="tl-personaje" style="color:${colorDe(t.personaje)}">${escapar(t.personaje)}</span>
        <span class="tl-texto">${escapar(recortar(t.texto, 90))}</span>
      </div>`).join("");

    $("linea-tiempo").querySelectorAll(".tl-item").forEach((el) => {
      el.addEventListener("click", () => {
        audio.currentTime = parseFloat(el.dataset.inicio);
        audio.play().catch(() => {});
      });
    });

    audio.ontimeupdate = () => {
      const t = audio.currentTime;
      const activo = G.tiempos.findIndex((x) => t >= x.inicio && t < x.fin);
      $("linea-tiempo").querySelectorAll(".tl-item").forEach((el, i) =>
        el.classList.toggle("activo", i === activo));
    };
    $("resultado-guion").classList.remove("oculto");
  }

  const reloj = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

  /* ==================== Guardar y recuperar guiones ==================== */
  async function guardarGuion() {
    const g = await api("/api/guiones", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: G.idGuion,
        titulo: $("titulo-guion").value.trim() || "Guion sin título",
        idioma: $("idioma").value,
        segmentos: G.segmentos,
      }),
    });
    G.idGuion = g.id;
    await listarGuiones();
    const boton = $("btn-guardar-guion");
    boton.textContent = "✓ Guardado";
    setTimeout(() => { boton.textContent = "Guardar guion"; }, 1800);
  }

  async function listarGuiones() {
    const guiones = await api("/api/guiones");
    $("guiones-guardados").innerHTML =
      [`<option value="">Abrir un guion guardado…</option>`]
        .concat(guiones.map((g) =>
          `<option value="${g.id}" ${g.id === G.idGuion ? "selected" : ""}>` +
          `${escapar(g.titulo)} (${g.segmentos})</option>`)).join("");
  }

  async function abrirGuion(id) {
    if (!id) return;
    const g = await api(`/api/guiones/${id}`);
    G.idGuion = g.id;
    $("titulo-guion").value = g.titulo;
    G.segmentos = g.segmentos;
    pintarTodo();
  }

  function descargarTexto() {
    const texto = G.segmentos.map((s) => `[${s.personaje}] ${s.texto}`).join("\n");
    const url = URL.createObjectURL(new Blob([texto], { type: "text/plain;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = ($("titulo-guion").value.trim() || "guion") + ".txt";
    a.click();
    URL.revokeObjectURL(url);
  }

  /* ==================== Arranque ==================== */
  function conectar() {
    document.querySelectorAll(".modo").forEach((b) => {
      b.addEventListener("click", () => {
        document.querySelectorAll(".modo").forEach((o) => o.classList.remove("activa"));
        b.classList.add("activa");
        $("vista-simple").classList.toggle("oculto", b.dataset.vista !== "vista-simple");
        $("vista-guion").classList.toggle("oculto", b.dataset.vista !== "vista-guion");
      });
    });

    $("btn-llm-cargar").addEventListener("click", cargarModelo);
    $("btn-llm-probar").addEventListener("click", probarModelo);
    $("btn-llm-parar").addEventListener("click", async () => {
      await api("/api/llm/parar", { method: "POST" });
      pintarEstadoLLM({ cargado: false });
    });
    const guardarAjuste = (datos) => api("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(datos),
    }).catch(() => {});

    $("llm_temp").addEventListener("input", (e) => {
      $("v-llm_temp").textContent = Number(e.target.value).toFixed(2);
      guardarAjuste({ llm_temp: parseFloat(e.target.value) });
    });
    $("liberar_llm").addEventListener("change", (e) =>
      guardarAjuste({ liberar_llm: e.target.value }));

    $("btn-escribir").addEventListener("click", escribirGuion);
    $("btn-parar-escritura").addEventListener("click", async () => {
      if (G.tareaEscritura) await api(`/api/tarea/${G.tareaEscritura}/cancelar`, { method: "POST" });
    });
    $("btn-pegar").addEventListener("click", () => {
      $("borrador-caja").classList.remove("oculto");
      $("borrador-info").textContent =
        "Pega tu texto con el formato [PERSONAJE] frase y pulsa «Convertir en guion».";
      $("borrador").focus();
    });
    $("btn-usar-borrador").addEventListener("click", convertirBorrador);

    $("btn-repartir").addEventListener("click", () => { repartirAuto(true); pintarTodo(); });
    $("btn-anadir-seg").addEventListener("click", () => {
      const ultimo = G.segmentos[G.segmentos.length - 1];
      G.segmentos.push({
        personaje: ultimo?.personaje || "NARRADOR",
        texto: "", voz: ultimo?.voz || null, pausa_ms: 420,
      });
      pintarTodo();
      $("segmentos").lastElementChild?.querySelector(".seg-texto")?.focus();
    });

    $("btn-generar-guion").addEventListener("click", generarGuion);
    $("btn-cancelar-guion").addEventListener("click", async () => {
      if (G.tareaAudio) await api(`/api/tarea/${G.tareaAudio}/cancelar`, { method: "POST" });
    });
    $("btn-guardar-guion").addEventListener("click", guardarGuion);
    $("btn-vaciar-guion").addEventListener("click", () => {
      if (G.segmentos.length && !confirm("¿Vaciar el guion actual?")) return;
      G.segmentos = [];
      G.idGuion = null;
      $("titulo-guion").value = "";
      $("guiones-guardados").value = "";
      pintarTodo();
    });
    $("guiones-guardados").addEventListener("change", (e) => abrirGuion(e.target.value));
    $("descargar-txt").addEventListener("click", descargarTexto);

    window.App.alCambiarVoces.push((voces) => {
      G.voces = voces;
      if (G.segmentos.length) pintarTodo();
    });
    window.App.alCargarEstado.push((s) => {
      pintarDispositivosLLM(s);
      cargarCatalogoLLM();
    });
  }

  conectar();
  api("/api/llm/estado").then(pintarEstadoLLM).catch(() => {});
  listarGuiones().catch(() => {});
})();
