# Modelos de texto (opcional)

Esta carpeta es para los `.gguf` de **modelos de lenguaje** que quieras usar en
el modo **Guion multivoz**, donde el modelo escribe la historia.

No hace falta poner nada aquí si ya usas **Ollama**: la aplicación encuentra sola
los modelos que tengas descargados y se los pasa a `llama-server`. Los blobs de
Ollama son GGUF normales, así que no se copia ni se duplica nada.

Suelta aquí un `.gguf` solo si lo has descargado por tu cuenta (de Hugging Face,
por ejemplo) y no está en Ollama. Aparecerá en el desplegable como «Carpeta
local».

> Ojo: no todo lo que empaqueta Ollama carga en llama.cpp. El botón **Comprobar
> compatibilidad** lo verifica y te enseña el error exacto si falla.
> Ver [docs/guion-multivoz.md](../docs/guion-multivoz.md).
