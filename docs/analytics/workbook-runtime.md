# Workbook runtime configuration

Native workbook stages use the existing `xlsx-win` adapter selected by an
explicit descriptor. Configure the release builder with
`--workbook-runtime PATH` on `build` or on `stage --role workbook`.

The descriptor is schema 1 and paths may be relative to the descriptor file:

```json
{
  "schema_version": 1,
  "adapter": "xlsx-win",
  "python_executable": "runtime/venv/Scripts/python.exe",
  "adapter_root": "xlsx-win",
  "supervisor_executable": "xlsx-win/supervisor/XlsxWinSupervisor/bin/Release/net10.0-windows/XlsxWinSupervisor.exe",
  "worker_executable": "xlsx-win/supervisor/XlsxWinWorker/bin/Release/net10.0-windows/XlsxWinWorker.exe"
}
```

The descriptor is authoritative. The builder does not select a runtime from
the user home directory, `PATH`, ambient `XLSXWIN_*` variables, or file
modification time. It validates the adapter layout, schema 2.0 contract,
configured interpreter dependencies, and native executable siblings before
workbook execution. Non-workbook stages do not require this descriptor.

Runtime execution receipts are local execution evidence only; they are not
included in recipe, job, canonical result, report, or financial binding
identity.
