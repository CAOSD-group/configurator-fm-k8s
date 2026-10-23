# Piloto: generación automática de manifiestos Kubernetes desde UVL

## Qué hace y qué no hace

`experiment_runner.py` prepara casos de prueba desde `pilot_profile.json` y ejecuta **únicamente la generación** de manifiestos mediante las funciones del generador `main_k8s01.py`.

- Resuelve el `apiVersion`/`kind` del recurso con `MetadataMapper` (`metadata_parser01.py`).
- Traduce las rutas `configPath` y los valores del perfil a nombres reales de features usando `serialization_metadata_*.json`.
- Crea un JSON parcial por caso y deja que `main_k8s01.py` complete los ancestros, defaults y restricciones del modelo.
- Carga y traduce el UVL a SAT una sola vez por ejecución; completa cada caso por separado.
- Escribe un YAML y una configuración completa por caso generado, más un CSV de ejecución.

**No** hace validación de sintaxis YAML, validación de esquema, `kubectl`, mapeo inverso ni evaluación de la corrección del manifiesto. `status=generated` significa exclusivamente que el generador devolvió y escribió contenido no vacío.

Esta primera batería solo propone variaciones escalares y alternativas explícitas. Los arrays/mapas no forman parte del perfil inicial. No se garantiza que las configuraciones base sean manifiestos desplegables.

## Archivos

- `experiment_runner.py` — automatizador simplificado.
- `pilot_profile.json` — selección de objetos, propiedades y valores.
- `test_experiment_runner.py` — pruebas con modelos/metadatos simulados.

No es necesario modificar `convert04.py`, `metadata_parser01.py` ni `main_k8s01.py` para realizar esta prueba. Se reutilizan los módulos importándolos; los argumentos `--mapper` y `--generator` permiten indicar sus rutas reales.

## Requisitos

- Ejecutar con `python` **del entorno virtual donde funciona FlamaPy**, no necesariamente con `py`.
- Disponer de los JSON `descriptions_1-37_V2.json` y `serialization_metadata_1-37_V2.json` generados por el conversor.
- Disponer del UVL estructural y del generador compatible con las funciones `UVLReader`, `FmToPysat`, `Configuration`, `complete_and_minimize`, `render_manifest` y `write_manifest`.
- `metadata_parser01.py` utiliza `PyYAML` para importarse.

## 1. Prueba rápida: generar tres manifiestos

Desde la raíz del proyecto en PowerShell, corrigiendo las rutas según corresponda:

```powershell
python .\experiment_runner.py `
  --profile .\pilot_profile.json `
  --descriptions .\resources\model_generation\descriptions_1-37_V2.json `
  --metadata .\resources\model_generation\serialization_metadata_1-37_V2.json `
  --mapper .\scripts\mapping_files\metadata_parser01.py `
  --generator .\main_k8s01.py `
  --uvl .\variability_model\kubernetes_combined_1-37_V2.uvl `
  --output-dir .\experiments\smoke `
  --limit 3
```

Se prueban los tres primeros casos de `pilot_profile.json`:

1. Deployment base.
2. Deployment con `spec.strategy.type=RollingUpdate`.
3. Deployment con `spec.strategy.type=Recreate`.

Si una feature no se puede resolver con los metadatos, el caso queda como `skipped`. Si se produce una excepción al generar, queda como `failed` con su motivo. **Es posible que algunos casos fallen con el UVL real; esos fallos son el objetivo de la primera prueba.**

## 2. Generar los 12 casos

Repite el comando anterior quitando `--limit 3` y utilizando otra carpeta:

```powershell
  --output-dir .\experiments\pilot
```

El perfil contiene 12 casos propuestos: 2 bases y 10 variaciones individuales en Deployment y Service. La cantidad generada dependerá de las features y valores efectivamente representados en tu modelo.

## 3. Opcional: solo preparar los JSON parciales

Si antes quieres revisar los casos sin cargar el UVL, añade `--plan` al comando. En este modo no hace falta indicar `--generator` ni `--uvl`.

## Salida

```text
experiments/smoke/
├── partial_configs/         # JSON de decisiones parciales
├── full_configs/            # Configuración completada por FlamaPy
├── manifests/               # YAML generado
└── generation_results.csv   # Resultado de cada caso
```

Columnas relevantes del CSV:

- `case_id`, `kind`, `api_version`: identificación.
- `variation_point`, `requested_value`, `choice_feature`: variación solicitada.
- `status`: `planned`, `generated`, `skipped` o `failed`.
- `partial_json`, `full_config_json`, `manifest_yaml`: rutas de artefactos, si existen.
- `error`, `elapsed_seconds`: motivo de fallo y tiempo de generación.

El CSV se escribe inicialmente y se actualiza tras cada caso completado, para no perder los resultados anteriores si se interrumpe el proceso.

## Pruebas del propio automatizador

```powershell
python -m unittest -v test_experiment_runner
```

Estas pruebas son sintéticas: verifican la planificación y que el runner registra generación y errores, **no** la generación real contra Kubernetes 1.37.1.
