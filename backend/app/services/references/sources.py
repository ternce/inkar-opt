from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReferenceFilePayload:
    data_type: str
    filename: str
    content: bytes


class ReferenceImportSource:
    source_type = "unknown"

    def get_payloads(self) -> list[ReferenceFilePayload]:
        raise NotImplementedError


class ExcelReferenceSource(ReferenceImportSource):
    source_type = "excel"

    def __init__(self, payloads: list[ReferenceFilePayload]):
        self._payloads = payloads

    def get_payloads(self) -> list[ReferenceFilePayload]:
        return self._payloads


class SapReferenceSourcePlaceholder(ReferenceImportSource):
    source_type = "sap"

    def get_payloads(self) -> list[ReferenceFilePayload]:
        raise NotImplementedError("SAP reference source is not implemented yet")


def make_reference_source(source_type: str, payloads: list[ReferenceFilePayload]) -> ReferenceImportSource:
    if source_type == "excel":
        return ExcelReferenceSource(payloads)
    if source_type == "sap":
        return SapReferenceSourcePlaceholder()
    raise ValueError("unknown reference source")
