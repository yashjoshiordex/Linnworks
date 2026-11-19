"""Read the Linnworks AJC export CSV and change single cells without touching anything else.

Why not Python's csv module: re-writing a file with it can change quoting, line endings or
spacing, and the 3PL import expects the exact export format. Here each row keeps its original
bytes plus the position of every cell, so a swap replaces only the SKU / Fill SKU bytes and
the rest of the file stays byte-for-byte identical.

Columns are found by position (the export has two columns both called "Item Number"), and
the header must match HEADER exactly or the file is rejected.
Errors deliberately omit cell contents: input contains customer data.
"""

from dataclasses import dataclass
from collections.abc import Iterable, Mapping


HEADER = tuple((
    "Order Id,Received date,Customer name,Customer company,Address 1,Address 2,"
    "Address 3,Town,Region,Postcode,Country,Country code,Buyer phone number,"
    "Customer email address,Shipping service name,Shipping service vendor,"
    "Shipping service code,Source,SubSource,Reference number,Channel reference,"
    "External Reference,Order total,Total Discount,Order tax,Shipping cost,Currency,"
    "SKU,Item title,Quantity,Unit cost,Line total excluding tax,Line total,"
    "Line discount,Product Tax rate,Order Notes,Channel SKU,Original Title,"
    "Item Number,Delivery Start Date,Delivery End Date,Despatch By,Tax Id,Fill SKU,"
    "Item Number,ThirdPartyAccountNumber,ThirdPartyCountryCode,ThirdPartyPostalCode,"
    "GI_ShippingMethod,ShippingAccountType,GI_CarrierID,GI_OrderNumber,GI_OrderType,"
    "ShippingRule,GI_CustomerCode,GI_CarrierScac"
).split(","))
# 0-based column positions in HEADER (Order Id is 0; "Fill SKU" is 43).
SKU_COLUMN = 27
QUANTITY_COLUMN = 29
ITEM_NUMBER_COLUMNS = (38, 44)
BOM = b"\xef\xbb\xbf"  # UTF-8 byte-order mark some exports start with; kept as-is


class CsvFormatError(ValueError):
    """Invalid input or a replacement that cannot retain its quoting style."""


@dataclass(frozen=True)
class CsvRecord:
    """One CSV line: its exact bytes (raw), decoded cell values, and each cell's (start, end)
    byte position within raw."""

    raw: bytes
    values: tuple[str, ...]
    spans: tuple[tuple[int, int], ...]

    def replace(self, changes: Mapping[int, str]) -> bytes:
        """Keep all bytes except explicitly changed positional cells.

        Quoted cells stay quoted. An unquoted replacement requiring quotes is
        rejected, rather than silently violating the export's quoting contract.
        """
        replacements = []
        for column, value in changes.items():
            if not isinstance(column, int) or not 0 <= column < len(self.values):
                raise CsvFormatError("Replacement column is outside the CSV layout")
            if not isinstance(value, str) or "\x00" in value:
                raise CsvFormatError("Invalid replacement value")
            if value == self.values[column]:
                continue
            start, end = self.spans[column]
            try:
                encoded = value.encode("utf-8")
            except UnicodeEncodeError:
                raise CsvFormatError("Replacement is not valid UTF-8") from None
            if self.raw[start:end].startswith(b'"'):
                encoded = b'"' + encoded.replace(b'"', b'""') + b'"'
            elif any(c in encoded for c in (b",", b'"', b"\r", b"\n")):
                raise CsvFormatError("Replacement requires a change in quoting style")
            replacements.append((start, end, encoded))
        result = self.raw
        for start, end, encoded in sorted(replacements, reverse=True):
            result = result[:start] + encoded + result[end:]
        return result


@dataclass(frozen=True)
class CsvDocument:
    """A whole export file: optional BOM, header row and data rows (one row per order line)."""

    prefix: bytes
    header: CsvRecord
    rows: tuple[CsvRecord, ...]

    def render(self, rows: Iterable[CsvRecord] | None = None) -> bytes:
        """File bytes for the given rows (default: all), with the original BOM and header."""
        return self.prefix + self.header.raw + b"".join(
            row.raw for row in (self.rows if rows is None else rows)
        )

    def orders(self) -> dict[str, tuple[CsvRecord, ...]]:
        """Rows grouped by order: {order id: its lines}. Decisions are made per order."""
        grouped: dict[str, list[CsvRecord]] = {}
        for row in self.rows:
            # Canonicalize numeric IDs so 001 and 1 cannot dispatch separately.
            grouped.setdefault(str(int(row.values[0])), []).append(row)
        return {key: tuple(rows) for key, rows in grouped.items()}


def _records(data: bytes) -> tuple[CsvRecord, ...]:
    """Split CSV bytes into records, remembering each cell's byte span.

    Standard CSV rules: cells separated by ",", optional double quotes, "" inside quotes is one
    quote, quoted cells may contain commas and line breaks, rows end with LF or CRLF.
    (Byte values: 34 = '"', 44 = ',', 10 = LF, 13 = CR.)
    """
    records = []
    offset = 0
    while offset < len(data):
        beginning = offset
        values = []
        spans = []
        while True:
            start = offset
            if offset < len(data) and data[offset] == 34:
                offset += 1
                while True:
                    if offset >= len(data):
                        raise CsvFormatError("Unterminated quoted field")
                    if data[offset] == 34:
                        if offset + 1 < len(data) and data[offset + 1] == 34:
                            offset += 2
                            continue
                        offset += 1
                        break
                    offset += 1
                cell = data[start + 1:offset - 1].replace(b'""', b'"')
            else:
                while offset < len(data) and data[offset] not in (44, 10, 13):
                    if data[offset] == 34:
                        raise CsvFormatError("Quote inside an unquoted field")
                    offset += 1
                cell = data[start:offset]
            spans.append((start - beginning, offset - beginning))
            values.append(cell.decode("utf-8"))
            if offset == len(data):
                break
            delimiter = data[offset]
            offset += 1
            if delimiter == 44:
                continue
            if delimiter == 13:
                if offset < len(data) and data[offset] == 10:
                    offset += 1
                break
            if delimiter == 10:
                break
            raise CsvFormatError("Unexpected bytes after quoted field")
        records.append(CsvRecord(data[beginning:offset], tuple(values), tuple(spans)))
    return tuple(records)


def parse_csv(data: bytes) -> CsvDocument:
    """Parse and validate a whole export before returning any rows.

    Rejects (CsvFormatError) a file that isn't valid UTF-8, has a different header, a row with
    the wrong number of columns, a non-numeric order ID or quantity, or an empty SKU. A bad
    file is never partly processed.
    """
    if b"\x00" in data:
        raise CsvFormatError("NUL byte in CSV input")
    prefix = BOM if data.startswith(BOM) else b""
    try:
        records = _records(data[len(prefix):])
    except UnicodeDecodeError:
        raise CsvFormatError("CSV input is not valid UTF-8") from None
    if not records or records[0].values != HEADER:
        raise CsvFormatError("CSV header does not match the approved positional layout")
    for index, row in enumerate(records[1:], start=2):
        if len(row.values) != len(HEADER):
            raise CsvFormatError(f"CSV record {index} has an invalid column count")
        order_id = row.values[0]
        quantity = row.values[QUANTITY_COLUMN]
        if not order_id.isascii() or not order_id.isdigit() or len(order_id) > 10 or not 0 < int(order_id) <= 2147483647:
            raise CsvFormatError(f"CSV record {index} has an invalid numeric order ID")
        if not quantity.isascii() or not quantity.isdigit() or len(quantity) > 10 or not 0 < int(quantity) <= 2147483647:
            raise CsvFormatError(f"CSV record {index} has an invalid quantity")
        if not row.values[SKU_COLUMN].strip():
            raise CsvFormatError(f"CSV record {index} has an empty SKU")
    return CsvDocument(prefix, records[0], records[1:])
