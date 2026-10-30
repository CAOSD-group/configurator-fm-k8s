# Pipeline experimental v2 — métricas y modo point-only

#### Generar manifiestos de los parciales:

python .\generate_manifests.py --partial-dir ..\..\resources\experiments\deployment\partial_plan\partial_configs --uvl ..\..\variability_model\kubernetes_combined_1-37_V3_semantic.uvl --generator ..\..\main_k8s03_arr.py --root-feature io_k8s_api_apps_v1_Deployment --output-dir ..\..\resources\experiments\deployment\generation_run02 --variability-comments

#### Evaluar archivos generados para obtener análisis core y extendido:

python .\evaluation_manifests02.py --manifest-dir ..\..\resources\experiments\deployment\generation_run\manifests --output-dir ..\..\resources\experiments\deployment\evaluation_fm_variants --source-id flamapy_variants --expected-kind Deployment --expected-api-version apps/v1 --descriptions ..\..\resources\model_generation\descriptions_1-37_V3.json --metadata ..\..\resources\model_generation\serialization_metadata_1-37_V3.json --mapper ..\..\scripts\mapping_files\metadata_parser01.py --uvl ..\..\variability_model\kubernetes_combined_1-37_V3_semantic.uvl --root-feature io_k8s_api_apps_v1_Deployment --full-config-dir ..\..\resources\experiments\deployment\generation_run\full_configs --fm-sat-mode full --kubectl-mode none


## I. Primer experimento: evaluar los YAML ya generados

Ejecutar desde `scripts\experiment_generation`:

```powershell
python .\evaluate_manifests_v2.py `
  --manifest-dir ..\..\resources\experiments\deployment\generation_run\manifests `
  --output-dir ..\..\resources\experiments\deployment\evaluation_fm_v2 `
  --source-id flamapy `
  --expected-kind Deployment `
  --expected-api-version apps/v1 `
  --metadata ..\..\resources\model_generation\serialization_metadata_1-37_V2.json `
  --uvl ..\..\variability_model\kubernetes_combined_1-37_V3_semantic.uvl `
  --root-feature io_k8s_api_apps_v1_Deployment `
  --full-config-dir ..\..\resources\experiments\deployment\generation_run\full_configs
```

Salidas: `evaluation_core.csv` (18 columnas principales), `evaluation_metrics.csv` (50 columnas de diagnóstico), y `evaluation_summary.json`.

**Precondición para las métricas exactas `n_*`:** debe existir `full_configs/<nombre-del-yaml>.json`, y hay que pasar `--uvl`, `--root-feature` y `--full-config-dir`. Si no existe, `n_*` queda vacío y `uvl_count_source=full_configuration_json_missing`. Es un error metodológico reemplazar los valores ausentes por cero.

**Definiciones precisas:**

- `n_features`: nº de features seleccionadas en el JSON completo del subárbol, excluyendo `false`/`null`. Incluye features estructurales y alternativas; no equivale a nº de campos YAML.
- `n_variation_points`: nº de *grupos de relación* `alternative` cuyo padre está seleccionado en la configuración. Esto contabiliza grupos activos, no la totalidad de los 119 disponibles en Deployment.
- `n_variants`: nº de features hijas de esas relaciones `alternative` que están seleccionadas. En un FM válido con XOR, normalmente `n_variants == n_variation_points`.
- `n_defaults`: nº de features seleccionadas marcadas con atributo UVL `default` dentro del subárbol. NO demuestra por sí solo que las haya introducido el algoritmo; pueden haber sido elegidas explícitamente.
- `pct_defaults`: `100 * n_defaults / uvl_defaults_available`, donde el denominador es el catálogo completo de defaults de la raíz, no solo los que estaban accesibles bajo los padres activos. La métrica `uvl_alternative_defaults_active_pct` es la proporción contextual para los grupos alternatives ACTIVOS que tienen alguna opción marcada default.
- Los YAML externos no contienen normalmente el JSON de selección del FM: el evaluador deja `n_*` vacío para esos casos. `yaml_matched_*` da una estimación observable bajo el mapeo YAML→UVL; no es equivalente a los contadores exactos.

## II. Segundo experimento: planificar un JSON por punto de variación

No se necesita repetir el descubrimiento ni alterar las 114 configuraciones originales. Con el mismo `auto_profile.json` que se utilizó en el experimento anterior:

```powershell
python .\experiment_runner_v5.py `
  --profile ..\..\resources\experiments\deployment\auto_profile.json `
  --descriptions ..\..\resources\model_generation\descriptions_1-37_V2.json `
  --metadata ..\..\resources\model_generation\serialization_metadata_1-37_V2.json `
  --mapper ..\..\scripts\mapping_files\metadata_parser01.py `
  --output-dir ..\..\resources\experiments\deployment\point_only_plan `
  --case-mode points `
  --plan
```

Modo `variants` (valor predeterminado) conserva la estrategia anterior. El modo `points` hace deduplicación por `point_feature`. Su número de casos será **1 base + número de puntos alternative que son planificables en el perfil**, que es menor o igual al número de relaciones `alternative` detectadas. NO se deben esperar necesariamente 119 casos, porque 207 variantes no se han materializado todavía.

Caso escalar típico:

```json
{
  "io_k8s_api_apps_v1_Deployment_apiVersion": "apps/v1",
  "io_k8s_api_apps_v1_Deployment_kind": "Deployment",
  "io_k8s_api_apps_v1_Deployment_metadata_name": "fm-pilot-deployment",
  "io_k8s_api_apps_v1_Deployment_spec_strategy_type": true
}
```

Caso en un array: el input expresa el punto sin su variante concreta; la instancia sigue siendo necesaria para generar un array no vacío:

```json
{
  "io_k8s_api_apps_v1_Deployment_apiVersion": "apps/v1",
  "io_k8s_api_apps_v1_Deployment_kind": "Deployment",
  "io_k8s_api_apps_v1_Deployment_metadata_name": "fm-pilot-deployment",
  "io_k8s_api_apps_v1_Deployment_spec_template_spec_volumes_vsphereVolume_fsType": true,
  "io_k8s_api_apps_v1_Deployment_spec_template_spec_volumes": [
    {
      "io_k8s_api_apps_v1_Deployment_spec_template_spec_volumes_vsphereVolume": {
        "io_k8s_api_apps_v1_Deployment_spec_template_spec_volumes_vsphereVolume_fsType": true
      }
    }
  ]
}
```

Para generar YAML del **segundo experimento** y medir tiempos, emplear el generador temporizado v2:

```powershell
python .\generate_manifests_timed_v2.py `
  --partial-dir ..\..\resources\experiments\deployment\point_only_plan\partial_configs `
  --uvl ..\..\variability_model\kubernetes_combined_1-37_V3_semantic.uvl `
  --generator .\main_k8s05.py `
  --root-feature io_k8s_api_apps_v1_Deployment `
  --output-dir ..\..\resources\experiments\deployment\point_only_generation `
  --plan-csv ..\..\resources\experiments\deployment\point_only_plan\generation_results.csv `
  --variability-comments
```

La función nueva `materialize_selected_array_alternatives` traduce *solo para el renderizado* `{fsType:true}` a `{fsType:{fsType_ext4:true}}` si el modelo completo selecciona `ext4`. Registra `array_points_resolved` y `time_resolve_array_points_s` en `generation_times.csv`. Sin esta operación el generador original representaría en el YAML `fsType: true`, valor incorrecto para ese campo. Si la selección no se resuelve de manera única, el caso falla con error explícito. Esto no inventa `volumePath`, `name` ni otros valores obligatorios para la API.

### Importante para la comparación LLM–FM

- El contexto lógico compartido es la indicación de la propiedad o punto de variación **sin fijar su alternativa**. No equivale todavía a un prompt LLM definitivo: el mensaje y el contexto deben especificar claramente la misma propiedad y restricciones de Deployment.
- El FM seleccionará un valor `default` compatible con SAT **si el modelo contiene esa marca**. Si no hay marca, SAT/MaxSAT puede elegir otra alternativa satisfacible; no hay garantía universal de que sea el valor por defecto de Kubernetes.
- En `volumes[]`, la instancia de la colección sigue siendo obligatoria para expresar el lugar donde aparece ese punto. El FM actual no modela la cardinalidad ni los datos por instancia en SAT. Por ello la comparación requiere cautela.
- Las configuraciones parciales del modo variants y points no deben mezclarse en un mismo directorio.

## III. Diccionario completo de las columnas del evaluador (50)

La columna **Nivel** indica: **central** (métrica principal, incluida en el CSV breve), **diagnóstico** (auditoría, interpretación o errores), **opcional** (solo si existen metadatos, JSON completo, kubectl o evaluación adicional). Algunas columnas antiguas `uvl_selected_*` son redundantes con los nuevos alias `n_*`: se mantienen por compatibilidad.

| Columna | Nivel | Significado |
|---|---|---|
| `case_id` | Central | Identificador del YAML, basado en nombre de archivo. |
| `source_id` | Central | Procedencia declarada: FM, LLM, etc. |
| `manifest_yaml` | Central | Ruta al manifiesto evaluado. |
| `full_config_json` | Diagnóstico | Ruta al JSON FM completo asociado, si existe. |
| `status` | Central | Estado global del análisis del fichero, no prueba de validez semántica. |
| `error` | Diagnóstico | Error de parseo/análisis, si ocurrió. |
| `n_features` | Central / FM | Features seleccionadas del JSON FM completo. |
| `n_variation_points` | Central / FM | Relaciones `alternative` activas (padre seleccionado). |
| `n_variants` | Central / FM | Hijos alternativos seleccionados. |
| `n_defaults` | Central / FM | Features seleccionadas marcadas `default`. |
| `pct_defaults` | Central / FM | `n_defaults / uvl_defaults_available * 100`. |
| `n_alternative_groups_resolved` | Diagnóstico / FM | Grupos activos con exactamente un hijo seleccionado. |
| `n_alternative_groups_unresolved` | Diagnóstico / FM | Grupos activos sin hijo seleccionado en el JSON completo. |
| `yaml_syntax_valid` | Central | PyYAML puede parsear el fichero; no comprueba OpenAPI. |
| `yaml_documents` | Diagnóstico | Nº de documentos YAML (`---`) encontrados. |
| `yaml_kind` | Diagnóstico | `kind` leído, en caso de YAML de un solo documento. |
| `yaml_api_version` | Diagnóstico | `apiVersion` leído, en caso de un solo documento. |
| `yaml_expected_gvk_ok` | Diagnóstico | Si el YAML declara el GVK esperado por el experimento. |
| `yaml_distinct_paths` | Diagnóstico | Nº de rutas YAML diferentes observadas, con `[]` para items. |
| `yaml_scalar_occurrences` | Diagnóstico | Nº de valores escalares recorridos (con repeticiones). |
| `yaml_list_item_occurrences` | Diagnóstico | Nº de elementos de listas observados. |
| `yaml_object_occurrences` | Diagnóstico | Nº de objetos/mappings observados. |
| `yaml_metadata_matched_paths` | Diagnóstico | Rutas observadas que tienen correspondencia en metadata. |
| `yaml_metadata_unmatched_paths` | Diagnóstico | Rutas observadas sin correspondencia (NO equivale a alucinaciones). |
| `yaml_metadata_match_pct` | Diagnóstico | Porcentaje de rutas observadas reconocidas por metadata. |
| `yaml_matched_features` | Opcional | Nº de features UVL inferidas conservadoramente del YAML. |
| `yaml_matched_alternative_variants` | Opcional | Variantes alternative observables por valor y metadata. |
| `yaml_matched_defaults` | Opcional | Features observables marcadas `default`. |
| `uvl_count_source` | Diagnóstico | Procedencia del conteo exacto (`full_configuration_json`), o falta de JSON. |
| `uvl_selected_features` | Diagnóstico / FM | Alias histórico de `n_features`. |
| `uvl_selected_alternative_groups` | Diagnóstico / FM | Alias de `n_variation_points`. |
| `uvl_selected_alternative_variants` | Diagnóstico / FM | Alias de `n_variants`. |
| `uvl_selected_defaults` | Diagnóstico / FM | Alias de `n_defaults`. |
| `uvl_default_selection_pct` | Diagnóstico / FM | Alias de `pct_defaults`. |
| `uvl_alternative_defaults_active_pct` | Opcional / FM | % de grupos alternative activos con default disponible cuya variante default está seleccionada. |
| `uvl_available_alternative_groups` | Diagnóstico / FM | Total de grupos alternative en el subárbol, activos o no. |
| `uvl_available_alternative_variants` | Diagnóstico / FM | Total de hijos de todos los grupos alternative disponibles. |
| `uvl_defaults_available` | Diagnóstico / FM | Nº total de features con marca UVL `default` en el subárbol. |
| `schema_valid` | Central / opcional | Resultado de validación de esquema con kubectl; vacío si no se ejecuta/no es concluyente. |
| `semantic_valid` | Central / opcional | Resultado semántico según validación Kubernetes de servidor, cuando es concluyente. |
| `validation_status` | Central | `not_run`, `passed`, `rejected`, `unavailable`, `error`. |
| `validation_detail` | Diagnóstico | Mensaje que explica resultado de kubectl. |
| `hallucinations` | Central futura | Conteo aún no implementado; vacío no significa 0. |
| `hallucinations_status` | Central | Estado de la futura evaluación de alucinaciones. |
| `time_read_yaml_s` | Diagnóstico | Segundos para leer fichero YAML. |
| `time_yaml_parse_s` | Diagnóstico | Segundos para parsear YAML. |
| `time_structure_s` | Diagnóstico | Segundos para recorrer YAML y mapear metadata. |
| `time_model_metrics_s` | Diagnóstico | Segundos para calcular métricas UVL del caso. |
| `time_validation_s` | Diagnóstico | Segundos de ejecución de kubectl, si se configuró. |
| `time_evaluation_total_s` | Diagnóstico | Segundos totales del análisis, separados del coste de generación. |

## IV. Qué métricas no deben confundirse

1. Un `false` en `yaml_syntax_valid` es fallo sintáctico; un `true` NO demuestra validez Kubernetes.
2. `schema_valid` y `semantic_valid` pueden quedar vacíos. `kubectl --dry-run=server` evalúa el estado real del API server y no constituye necesariamente un oráculo puramente de esquema o universal entre versiones de Kubernetes.
3. `n_features` exige el JSON FM completo y representa features UVL, incluidas estructuras abstractas y seleccionadas que quizá no producen un campo en YAML. No se puede comparar directamente con `yaml_matched_features` de otro origen.
4. `n_variation_points` es **nº de grupos alternative activos**. La cantidad total de VPs no es una característica del YAML, sino del modelo y del subárbol.
5. `hallucinations` permanece vacío para ambos orígenes hasta definir un verificador con criterios repetibles.

## V. Pruebas

```powershell
python .\test_variability_pipeline_v2.py
```

Son pruebas sintéticas de planificación, materialización de alternativas y conteo de métricas; NO se ha ejecutado aquí el modelo UVL V3 completo ni SAT ni validación de servidor. Para iniciar la segunda fase, ejecutar primero `--plan` y revisar los JSON antes de generar YAML.
