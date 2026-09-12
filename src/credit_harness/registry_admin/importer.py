"""Explicit, bounded XLSX schema; source memberships never imply Agent access."""
from io import BytesIO
from pathlib import PurePath
from zipfile import ZipFile
from hashlib import sha256
from openpyxl import load_workbook
from defusedxml.ElementTree import fromstring
from .models import CompanySystemRecord, CompanySystemMembership, SourceMetadata, AdminError

HEADERS = {
    "system_code": ("system_code", "系统简称", "系统编码", "系统英文名", "系统英文名称", "英文名称"),
    "system_name": ("system_name", "系统名称", "系统中文名", "系统中文名称", "中文名称"),
    "inventory_group": ("inventory_group", "系统分类", "系统类别", "system_category"),
    "system_type_label": ("system_type_label", "类型"),
    "description": ("description", "系统简要", "系统简介", "简介", "系统描述"),
    "main_functions": ("main_functions", "系统主要功能", "系统功能", "主要功能", "主要功能说明"),
}
COMPANY_SHEETS = frozenset(("九里云系统", "融担系统"))
MASTER_FIELDS = ("system_code", "system_name", "system_type_label", "description", "main_functions")


def parse_inventory(content, filename, actor, now):
    if not filename or not filename.lower().endswith(".xlsx") or len(content) > 5_000_000:
        raise AdminError("INVALID_WORKBOOK", 422)
    try:
        with ZipFile(BytesIO(content)) as z:
            entries = z.infolist()
            if len(entries) > 300 or sum(i.file_size for i in entries) > 25_000_000:
                raise ValueError()
            if any("vbaproject" in i.filename.lower() or "externallinks/" in i.filename.lower() for i in entries):
                raise ValueError()
            for entry in entries:
                if entry.filename.lower().endswith(".rels"):
                    if any(e.attrib.get("TargetMode", "").lower() == "external" for e in fromstring(z.read(entry)).iter()):
                        raise ValueError()
        book = load_workbook(BytesIO(content), read_only=True, data_only=False, keep_links=False)
        masters, invalid, conflicts, groups = {}, [], [], set()
        source_rows = 0
        file_hash = sha256(content).hexdigest()
        try:
            for sheet in book:
                # Bound the actual XML stream, not potentially misleading dimensions.
                sheet.reset_dimensions()
                stream = sheet.iter_rows()
                first = next(stream, ())
                if len(first) > 100 or any(c.data_type == "f" for c in first):
                    raise ValueError()
                headers = [str(c.value or "").strip() for c in first]
                indices = {k: [i for i, h in enumerate(headers) if h in names] for k, names in HEADERS.items()}
                if any(len(v) > 1 for v in indices.values()):
                    invalid.append({"row_ref": sheet.title + "!1", "code": "AMBIGUOUS_HEADERS"})
                    continue
                indices = {k: v[0] if v else None for k, v in indices.items()}
                if indices["system_code"] is None or indices["system_name"] is None:
                    invalid.append({"row_ref": sheet.title + "!1", "code": "REQUIRED_HEADERS_MISSING"})
                    continue
                group = sheet.title
                for n, cells in enumerate(stream, 2):
                    if n > 5001 or len(cells) > 100:
                        raise ValueError()
                    if all(c.value is None for c in cells):
                        continue
                    source_rows += 1
                    if source_rows > 5000:
                        raise ValueError()
                    ref = f"{sheet.title}!{n}"
                    try:
                        if any(c.data_type == "f" for c in cells):
                            raise ValueError()
                        values = {k: str(cells[i].value if cells[i].value is not None else "").strip()
                                  if i is not None and i < len(cells) else "" for k, i in indices.items()}
                        if values["inventory_group"]:
                            group = values["inventory_group"]
                        # Known company sheets have fixed group semantics; generic legacy
                        # sheets retain explicit groups. Never infer the system type.
                        if (sheet.title in COMPANY_SHEETS or group in COMPANY_SHEETS) and group != sheet.title:
                            invalid.append({"row_ref": ref, "code": "INVALID_INVENTORY_GROUP"})
                            continue
                        groups.add(group)
                        membership = CompanySystemMembership(system_code=values["system_code"], inventory_group=group,
                            source_metadata=SourceMetadata(source_file_name=PurePath(filename.replace('\\', '/')).name[:200],
                                source_file_hash=file_hash, source_row_ref=ref, imported_at=now, imported_by=actor))
                        row = CompanySystemRecord(**{k: values[k] for k in MASTER_FIELDS}, memberships=(membership,)).model_dump(mode="json")
                        previous = masters.get(row["system_code"])
                        if previous is None:
                            masters[row["system_code"]] = row
                        elif any(previous[k] != row[k] for k in MASTER_FIELDS):
                            conflicts.append(dict(system_code=row["system_code"], code="CONFLICTING_SYSTEM_DEFINITION",
                                row_refs=[previous["memberships"][0]["source_metadata"]["source_row_ref"], ref]))
                        else:
                            previous["memberships"].extend(row["memberships"])
                    except ValueError:
                        invalid.append({"row_ref": ref, "code": "INVALID_ROW"})
            sheet_count = len(book.sheetnames)
        finally:
            book.close()
        rows = [masters[k] for k in sorted(masters)]
        return dict(rows=rows, invalid=invalid, conflicts=conflicts, source_rows=source_rows,
            source_sheet_count=sheet_count, unique_systems=len(rows), groups=sorted(groups),
            shared_systems=sorted(r["system_code"] for r in rows if len({m["inventory_group"] for m in r["memberships"]}) > 1),
            source_file_hash=file_hash)
    except AdminError:
        raise
    except Exception:
        raise AdminError("INVALID_WORKBOOK", 422) from None
