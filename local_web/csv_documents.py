"""Bounded CSV-to-text conversion; no network, disk, or credential access."""
import csv
import io
import json

MAX_ROWS = 5000  # Data records; the header is separate.
MAX_COLUMNS = 50
MAX_CELL_CHARS = 4000


class CSVError(ValueError):
    def __init__(self, category, status=422):
        self.category, self.status = category, status
        super().__init__(category)


def _check_quotes(text):
    # csv.reader(strict=True) still accepts quotes inside unquoted fields.
    # Reject those as well, without changing the process-global CSV dialect.
    state = 'start'
    for char in text:
        if state == 'quoted':
            if char == '"':
                state = 'closed'
        elif state == 'closed':
            if char == '"':
                state = 'quoted'  # Escaped quote.
            elif char in ',\r\n':
                state = 'start'
            else:
                raise CSVError('invalid_csv')
        elif char in ',\r\n':
            state = 'start'
        elif char == '"':
            if state != 'start':
                raise CSVError('invalid_csv')
            state = 'quoted'
        else:
            state = 'unquoted'
    if state == 'quoted':
        raise CSVError('invalid_csv')


def csv_to_text(name, data, max_bytes=5 * 1024 * 1024):
    """Keep strings verbatim using JSON escapes, with logical and physical rows."""
    if not data or len(data) > max_bytes:
        raise CSVError('invalid_file_size', 413)
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        raise CSVError('invalid_text_encoding') from None
    if any(ord(c) < 32 and c not in '\r\n\t' for c in text):
        raise CSVError('invalid_text')
    _check_quotes(text)
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    output = bytearray()

    def quote(value):
        return json.dumps(value, ensure_ascii=False)

    def emit(line):
        encoded = (line + '\n').encode('utf-8')
        if len(output) + len(encoded) > max_bytes:
            raise CSVError('converted_csv_too_large', 413)
        output.extend(encoded)

    def check_cells(row):
        if any(len(cell) > MAX_CELL_CHARS for cell in row):
            raise CSVError('csv_cell_too_large', 413)

    try:
        headers = next(reader, None)
        if not headers:
            raise CSVError('invalid_csv_header')
        if len(headers) > MAX_COLUMNS:
            raise CSVError('csv_too_many_columns', 413)
        check_cells(headers)
        normalized = [header.strip() for header in headers]
        if any(not header for header in normalized) or len(set(normalized)) != len(headers):
            raise CSVError('invalid_csv_header')
        emit('CSV text representation v1')
        emit('Source CSV: ' + quote(name))
        emit(f'Header record 1 (source lines 1-{reader.line_num}): ' + quote(headers))
        previous_line = reader.line_num
        count = 0
        for count, row in enumerate(reader, 1):
            if count > MAX_ROWS:
                raise CSVError('csv_too_many_rows', 413)
            if len(row) != len(headers):
                raise CSVError('csv_column_count_mismatch')
            check_cells(row)
            emit(f'Data row {count} (CSV record {count + 1}; source lines {previous_line + 1}-{reader.line_num}):')
            for column, (header, cell) in enumerate(zip(headers, row), 1):
                emit(f'  Column {column} {quote(header)}: {quote(cell)}')
            previous_line = reader.line_num
        if not count:
            raise CSVError('csv_no_data_rows')
    except csv.Error:
        # Never propagate parser messages containing untrusted data.
        raise CSVError('invalid_csv') from None
    return bytes(output)
