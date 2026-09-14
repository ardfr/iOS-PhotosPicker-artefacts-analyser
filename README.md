# iOS PhotosPicker artefacts analyser
A digital forensic research tool for analysing iOS Photo Picker artefacts and correlating identified photo references with records contained within Photos.sqlite.
A digital forensic research tool for analysing iOS Photo Picker artefacts and correlating identified photo references with records contained within Photos.sqlite.

iOS Photo Picker Analyser is designed to assist digital forensic examiners in identifying photographs referenced through iOS Photo Picker artefacts and correlating those references back to the device's Photos library.

The tool extracts relevant Photo Picker records, identifies available asset references and attempts to correlate those references with corresponding records in Photos.sqlite.

The resulting data is presented in an Excel workbook to support forensic examination, validation and reporting.

Key Features:
Parses iOS Photo Picker artefacts.
Correlates Photo Picker references with Photos.sqlite.
Supports multiple correlation identifiers where available.
Identifies matched and unmatched records.
Preserves original source values.
Records the correlation method used for each match.
Generates an Excel forensic report.
Provides a summary of correlation results.
Maintains source database and table information for validation.
