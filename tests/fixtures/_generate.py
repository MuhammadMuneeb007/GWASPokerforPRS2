"""Regenerate the test fixtures.

Kept in the repository so the fixture bytes are reproducible and reviewable:
run ``python tests/fixtures/_generate.py`` from the repository root.

Each fixture targets a failure mode of the original implementation.
"""

from __future__ import annotations

import gzip
import io
import tarfile
import zipfile
from pathlib import Path

HERE = Path(__file__).parent


def _ssf_rows(count: int = 4000) -> bytes:
    """A GWAS-SSF-shaped table with enough rows to exceed a small probe."""
    rows = [
        b"chromosome\tbase_pair_location\teffect_allele\tother_allele\tbeta"
        b"\tstandard_error\teffect_allele_frequency\tp_value\tvariant_id\trsid"
    ]
    for i in range(count):
        position = 100000 + i * 37
        rows.append(
            f"1\t{position}\tA\tG\t{0.001 * (i % 50):.5f}\t0.0123\t0.45\t"
            f"{1e-3 / (i + 1):.3e}\t1_{position}_A_G\trs{500000 + i}".encode()
        )
    return b"\n".join(rows) + b"\n"


def main() -> None:
    # 1. Hash-comment preamble, tab-delimited. The canonical awkward case.
    (HERE / "comment_preamble.tsv").write_bytes(
        b"# GWAS summary statistics for migraine\n"
        b"# Generated 2024-01-01 by ExampleTool v1.2\n"
        b"CHR\tPOS\tSNP\tA1\tA2\tBETA\tSE\tP\n"
        b"1\t12345\trs1\tA\tG\t0.12\t0.03\t1e-6\n"
        b"1\t22222\trs2\tC\tT\t-0.05\t0.02\t3.4e-4\n"
        b"2\t99887\trs3\tG\tA\t0.01\t0.01\t0.42\n"
    )

    # 2. key=value preamble plus a blank line. pandas' comment='#' does not skip
    #    these, so v1 read "study=ABC" as the header.
    (HERE / "keyvalue_preamble.txt").write_bytes(
        b"study=ABC\n"
        b"author=XYZ\n"
        b"build=GRCh37\n"
        b"\n"
        b"MarkerName Allele1 Allele2 Effect StdErr P-value N\n"
        b"rs1 a g 0.1 0.02 1e-5 10000\n"
        b"rs2 c t -0.3 0.05 2e-9 10000\n"
        b"rs3 t a 0.05 0.03 0.09 9800\n"
    )

    # 3. Comma-delimited with quoted header cells.
    (HERE / "quoted_comma.csv").write_bytes(
        b'"variant_id","chromosome","base_pair_location","effect_allele",'
        b'"other_allele","beta","standard_error","p_value"\n'
        b'"rs1",1,12345,"A","G",0.12,0.03,1e-6\n'
        b'"rs2",1,22222,"C","T",-0.05,0.02,3.4e-4\n'
    )

    # 4. Latin-1: an author name in the preamble. Decoding this as UTF-8 raises.
    (HERE / "latin1_preamble.tsv").write_bytes(
        (
            "# Contributed by Björn Müller-Schäfer\n"
            "# Institut für Humangenetik\n"
            "CHR\tBP\tSNP\tEA\tNEA\tOR\tSE\tPVAL\n"
            "1\t12345\trs1\tA\tG\t1.05\t0.03\t1e-6\n"
            "2\t54321\trs2\tT\tC\t0.94\t0.04\t2e-5\n"
        ).encode("latin-1")
    )

    # 5. UTF-8 with a byte-order mark. Without BOM handling the first column
    #    name becomes "﻿chromosome" and never matches an alias.
    (HERE / "utf8_bom.tsv").write_bytes(
        b"\xef\xbb\xbf"
        b"chromosome\tbase_pair_location\teffect_allele\tother_allele\tbeta"
        b"\tstandard_error\tp_value\n"
        b"1\t12345\tA\tG\t0.12\t0.03\t1e-6\n"
        b"1\t22222\tC\tT\t-0.05\t0.02\t3.4e-4\n"
    )

    # 6. Twenty-five metadata rows then a blank line.
    lines = [f"## metadata line {i}: value {i}".encode() for i in range(1, 26)]
    lines += [
        b"",
        b"chromosome\tbase_pair_location\teffect_allele\tother_allele\tbeta"
        b"\tstandard_error\teffect_allele_frequency\tp_value",
        b"6\t63979\tT\tC\t-0.000160239\t0.0098139\t0.101252\t0.9869729",
        b"6\t63980\tG\tA\t-0.000917427\t0.00980197\t0.101732\t0.9254298",
        b"6\t73938\tG\tA\t0.0318572\t0.176681\t0.000831991\t0.8569094",
    ]
    (HERE / "many_metadata_rows.tsv").write_bytes(b"\n".join(lines) + b"\n")

    # 7. Gzip, large enough that a 4 KB probe truncates the stream mid-member.
    raw = _ssf_rows()
    (HERE / "ssf_like.tsv.gz").write_bytes(gzip.compress(raw, 6))
    (HERE / "ssf_like.tsv").write_bytes(raw[:4000])

    # 8. Space-delimited with runs of whitespace.
    (HERE / "space_delimited.txt").write_bytes(
        b"SNP CHR BP A1 A2 FRQ BETA SE P N\n"
        b"rs1 1 12345 A G 0.31 0.021 0.004 1.2e-07 150000\n"
        b"rs2 2 54321 T C 0.08 -0.013 0.006 3.0e-02 149800\n"
    )

    # 9. Semicolon-delimited, odds ratios rather than betas.
    (HERE / "semicolon.csv").write_bytes(
        b"chr;pos;snp;a1;a2;or;se;pval\n"
        b"1;12345;rs1;A;G;1.05;0.03;1e-6\n"
        b"2;54321;rs2;T;C;0.94;0.04;2e-5\n"
    )

    # 10. Headerless. Detection must not confidently invent one.
    (HERE / "headerless.tsv").write_bytes(
        b"1\t12345\trs1\tA\tG\t0.12\t0.03\t1e-6\n"
        b"1\t22222\trs2\tC\tT\t-0.05\t0.02\t3.4e-4\n"
        b"2\t99887\trs3\tG\tA\t0.01\t0.01\t0.42\n"
    )

    # 11. Zip holding the data file plus a much larger PDF -- the case where
    #     "largest member wins" picks the wrong file.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("results/sumstats.tsv", raw[:200000].decode())
        archive.writestr("results/manuscript.pdf", "%PDF-1.4\n" + "x" * 400000)
        archive.writestr("README.txt", "Supplementary data for Example et al.")
    (HERE / "archive.zip").write_bytes(buffer.getvalue())

    # 12. tar.gz.
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = raw[:150000]
        info = tarfile.TarInfo("study/sumstats.tsv")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
        note = b"Example archive"
        info = tarfile.TarInfo("study/readme.txt")
        info.size = len(note)
        archive.addfile(info, io.BytesIO(note))
    (HERE / "archive.tar.gz").write_bytes(buffer.getvalue())

    # 13. GWAS-SSF v1.0 metadata sidecar, shaped like a real one.
    (HERE / "ssf_meta.yaml").write_text(
        "# Study meta-data\n"
        "gwas_id: GCST90000001\n"
        "gwas_catalog_api: https://www.ebi.ac.uk/gwas/rest/api/studies/GCST90000001\n"
        "date_metadata_last_modified: 2026-01-15\n"
        "\n"
        "# Trait Information\n"
        "trait_description:\n"
        "  - Example trait\n"
        "\n"
        "# Genotyping Information\n"
        "genome_assembly: GRCh38\n"
        "coordinate_system: 1-based\n"
        "genotyping_technology:\n"
        "  - Genome-wide genotyping array\n"
        "\n"
        "# Sample Information\n"
        "samples:\n"
        "  - sample_ancestry_category:\n"
        "      - European\n"
        "    sample_size: 123456\n"
        "    case_control_study: true\n"
        "    case_count: 12345\n"
        "    control_count: 111111\n"
        "\n"
        "# Summary Statistic information\n"
        "data_file_name: GCST90000001.tsv.gz\n"
        "file_type: GWAS-SSF v1.0\n"
        "data_file_md5sum: 0123456789abcdef0123456789abcdef\n"
        "\n"
        "# Harmonization status\n"
        "is_harmonised: false\n"
        "is_sorted: false\n",
        encoding="utf-8",
    )

    # 14. pre-GWAS-SSF sidecar: readiness must NOT follow from the declaration.
    (HERE / "pre_ssf_meta.yaml").write_text(
        "# Study meta-data\n"
        "gwas_id: GCST90038646\n"
        "date_metadata_last_modified: 2025-01-14\n"
        "trait_description:\n"
        "  - Migraine\n"
        "genome_assembly: GRCh37\n"
        "samples:\n"
        "  - sample_ancestry_category:\n"
        "      - NR\n"
        "    sample_size: 484598\n"
        "data_file_name: GCST90038646_buildGRCh37.tsv\n"
        "file_type: pre-GWAS-SSF\n"
        "data_file_md5sum: 66b4c5f7091208cd518dd6ca2399c561\n"
        "is_harmonised: false\n"
        "is_sorted: false\n",
        encoding="utf-8",
    )

    # 15. Apache autoindex pages, as the resolver must parse them. The top level
    #     deliberately contains a 3.4G PDF that outranks the 1.2G data file by
    #     size alone.
    (HERE / "ftp_index.html").write_text(_TOP_LEVEL_INDEX, encoding="utf-8")
    (HERE / "ftp_index_harmonised.html").write_text(_HARMONISED_INDEX, encoding="utf-8")

    # 16. A share/landing page returned with HTTP 200 for a .gz URL. This is
    #     what an external run over 768 heterogeneous URLs actually received
    #     from moved files and share links -- not a corrupt archive.
    (HERE / "landing_page.html").write_text(
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta name="MobileOptimized" content="width" />\n'
        "<title>File not available</title>\n"
        "</head>\n"
        "<body>\n"
        '<div class="container"><p>This file has moved.</p></div>\n'
        "</body>\n"
        "</html>\n",
        encoding="utf-8",
    )

    # 17. An XML error document, as S3/GCS return for a missing key.
    (HERE / "error_document.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Error><Code>NoSuchKey</Code>"
        "<Message>The specified key does not exist.</Message></Error>\n",
        encoding="utf-8",
    )

    # 18. A tar whose first members are metadata: a directory entry and a README
    #     precede the data file. The old prefix parser skipped exactly one
    #     512-byte header and returned whatever followed, which is why valid
    #     ustar archives yielded no header even though the bytes were present.
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        directory = tarfile.TarInfo("study/")
        directory.type = tarfile.DIRTYPE
        directory.size = 0
        archive.addfile(directory)

        noise = b"Supplementary information for Example et al."
        readme = tarfile.TarInfo("study/README.txt")
        readme.size = len(noise)
        archive.addfile(readme, io.BytesIO(noise))

        payload = (
            b"MarkerName\tAllele1\tAllele2\tFreqAllele1HapMapCEU\tb\tse\tp\tN\n"
            b"rs1\ta\tg\t0.31\t0.021\t0.004\t1.2e-07\t150000\n"
            b"rs2\tc\tt\t0.08\t-0.013\t0.006\t3.0e-02\t149800\n"
        ) * 40
        data = tarfile.TarInfo("study/metaanalysis.tbl")
        data.size = len(payload)
        archive.addfile(data, io.BytesIO(payload))
    (HERE / "tar_with_metadata_first.tar").write_bytes(buffer.getvalue())

    # 19. Plain-text GWAS served under a .gz filename: the extension lies, but
    #     the bytes are perfectly readable.
    (HERE / "plaintext_named_gz.tsv.gz").write_bytes(
        b"CHR\tBP\tSNP\tA1\tA2\tBETA\tSE\tP\n"
        b"1\t12345\trs1\tA\tG\t0.12\t0.03\t1e-6\n"
        b"2\t99821\trs2\tC\tT\t-0.05\t0.02\t3.4e-4\n"
    )

    # 20. A zip laid out the way archives from supplementary material usually
    #     are: a macOS resource fork, a directory record and a PDF precede the
    #     data. Reading "the first local file header" -- the only thing possible
    #     without the central directory, which sits past the probe boundary --
    #     used to return the PDF.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__MACOSX/._study", b"\x00\x05\x16\x07resource fork")
        archive.writestr("study/", b"")
        archive.writestr("study/manuscript.pdf", b"%PDF-1.4\n" + b"binary padding " * 200)
        archive.writestr("study/README.txt", b"Supplementary information.\n" * 20)
        archive.writestr(
            "study/sumstats.tsv",
            b"CHR\tBP\tSNP\tA1\tA2\tBETA\tSE\tP\tN\n"
            + b"1\t12345\trs1\tA\tG\t0.12\t0.03\t1e-6\t100000\n" * 300,
        )
    (HERE / "zip_with_metadata_first.zip").write_bytes(buffer.getvalue())

    for path in sorted(HERE.glob("*")):
        if path.is_file() and path.name != "_generate.py":
            print(f"{path.name:34s} {path.stat().st_size:>9,} bytes")


_TOP_LEVEL_INDEX = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
<html>
 <head><title>Index of /pub/databases/gwas/summary_statistics/X/GCST90000001</title></head>
 <body>
<h1>Index of /pub/databases/gwas/summary_statistics/X/GCST90000001</h1>
  <table>
   <tr><th valign="top"><img src="/icons/blank.gif" alt="[ICO]"></th><th><a href="?C=N;O=D">Name</a></th><th><a href="?C=M;O=A">Last modified</a></th><th><a href="?C=S;O=A">Size</a></th><th><a href="?C=D;O=A">Description</a></th></tr>
   <tr><th colspan="5"><hr></th></tr>
<tr><td valign="top"><img src="/icons/back.gif" alt="[PARENTDIR]"></td><td><a href="/pub/databases/gwas/summary_statistics/X/">Parent Directory</a></td><td>&nbsp;</td><td align="right">  - </td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/text.gif" alt="[TXT]"></td><td><a href="GCST90000001_buildGRCh37.tsv">GCST90000001_buildGRCh37.tsv</a></td><td align="right">2025-02-07 19:49  </td><td align="right">1.2G</td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/unknown.gif" alt="[   ]"></td><td><a href="GCST90000001_buildGRCh37.tsv-meta.yaml">GCST90000001_buildGRCh37.tsv-meta.yaml</a></td><td align="right">2025-02-07 19:49  </td><td align="right">650 </td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/unknown.gif" alt="[   ]"></td><td><a href="Supplementary_manuscript.pdf">Supplementary_manuscript.pdf</a></td><td align="right">2025-02-07 19:49  </td><td align="right">3.4G</td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/folder.gif" alt="[DIR]"></td><td><a href="harmonised/">harmonised/</a></td><td align="right">2025-02-07 19:49  </td><td align="right">  - </td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/text.gif" alt="[TXT]"></td><td><a href="md5sum.txt">md5sum.txt</a></td><td align="right">2025-02-07 19:49  </td><td align="right">134 </td><td>&nbsp;</td></tr>
   <tr><th colspan="5"><hr></th></tr>
</table>
</body></html>
"""

_HARMONISED_INDEX = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
<html>
 <head><title>Index of /pub/databases/gwas/summary_statistics/X/GCST90000001/harmonised</title></head>
 <body>
<h1>Index of /pub/databases/gwas/summary_statistics/X/GCST90000001/harmonised</h1>
  <table>
   <tr><th valign="top"><img src="/icons/blank.gif" alt="[ICO]"></th><th><a href="?C=N;O=D">Name</a></th><th><a href="?C=M;O=A">Last modified</a></th><th><a href="?C=S;O=A">Size</a></th><th><a href="?C=D;O=A">Description</a></th></tr>
   <tr><th colspan="5"><hr></th></tr>
<tr><td valign="top"><img src="/icons/back.gif" alt="[PARENTDIR]"></td><td><a href="/pub/databases/gwas/summary_statistics/X/GCST90000001/">Parent Directory</a></td><td>&nbsp;</td><td align="right">  - </td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/compressed.gif" alt="[   ]"></td><td><a href="12345678-GCST90000001-EFO_0000001-Build37.f.tsv.gz">12345678-GCST90000001-EFO_0000001-Build37.f.tsv.gz</a></td><td align="right">2021-12-06 14:14  </td><td align="right">218M</td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/text.gif" alt="[TXT]"></td><td><a href="12345678-GCST90000001-EFO_0000001-Build37.f.tsv.gz-meta.yaml">12345678-GCST90000001-EFO_0000001-Build37.f.tsv.gz-meta.yaml</a></td><td align="right">2025-02-07 19:49  </td><td align="right">784 </td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/compressed.gif" alt="[   ]"></td><td><a href="12345678-GCST90000001-EFO_0000001.h.tsv.gz">12345678-GCST90000001-EFO_0000001.h.tsv.gz</a></td><td align="right">2021-12-06 14:14  </td><td align="right">378M</td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/text.gif" alt="[TXT]"></td><td><a href="12345678-GCST90000001-EFO_0000001.h.tsv.gz-meta.yaml">12345678-GCST90000001-EFO_0000001.h.tsv.gz-meta.yaml</a></td><td align="right">2025-02-07 19:49  </td><td align="right">776 </td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/text.gif" alt="[TXT]"></td><td><a href="md5sum.txt">md5sum.txt</a></td><td align="right">2025-02-07 19:49  </td><td align="right">384 </td><td>&nbsp;</td></tr>
   <tr><th colspan="5"><hr></th></tr>
</table>
</body></html>
"""


if __name__ == "__main__":
    main()
