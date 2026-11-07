"""Local file parsing; no OCR or model-based document processing.

入口 app.parsers.intake.parse_upload 做三重校验后分派到具体解析器，
统一产出 app.schemas.contracts.ParsedDocument。
"""
