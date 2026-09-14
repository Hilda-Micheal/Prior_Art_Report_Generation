"""
parser.py
=========
Transforms a raw Lens.org API hit into a clean flat dict
matching OUTPUT_FIELDS in config.py.
"""


def parse_patent(raw):
    """
    Extract and normalise fields from one raw Lens.org patent record.

    Field paths confirmed from live API responses:
        Title       : biblio.invention_title[].text  (prefer lang=en)
        Abstract    : abstract[].text                (prefer lang=en)
        IPC         : biblio.classifications_ipcr.classifications[].symbol
        CPC         : biblio.classifications_cpc.classifications[].symbol
        Cites       : biblio.references_cited.citations[].patcit.lens_id
        Cited by    : biblio.cited_by.patents[].lens_id
        Year        : date_published[:4]

    Returns:
        dict with keys matching OUTPUT_FIELDS
    """
    biblio  = raw.get("biblio", {})
    lens_id = raw.get("lens_id", "")

    # --- Title (prefer English) ---
    invention_title = biblio.get("invention_title", [])
    if isinstance(invention_title, list) and invention_title:
        en    = [t.get("text", "") for t in invention_title if t.get("lang") == "en"]
        title = en[0] if en else invention_title[0].get("text", "")
    elif isinstance(invention_title, dict):
        title = invention_title.get("text", "")
    else:
        title = str(invention_title) if invention_title else ""

    # --- Abstract (prefer English) ---
    abstract_list = raw.get("abstract", [])
    if isinstance(abstract_list, list) and abstract_list:
        en_abs   = [a.get("text", "") for a in abstract_list if a.get("lang") == "en"]
        abstract = en_abs[0] if en_abs else abstract_list[0].get("text", "")
    elif isinstance(abstract_list, dict):
        abstract = abstract_list.get("text", "")
    else:
        abstract = ""

    # --- IPC codes ---
    ipcr                = biblio.get("classifications_ipcr", {})
    ipc_classifications = ipcr.get("classifications", []) if isinstance(ipcr, dict) else []
    ipc_codes           = "; ".join(
        c.get("symbol", "") for c in ipc_classifications if c.get("symbol")
    )

    # --- CPC codes ---
    cpc_block           = biblio.get("classifications_cpc", {})
    cpc_classifications = cpc_block.get("classifications", []) if isinstance(cpc_block, dict) else []
    cpc_codes           = "; ".join(
        c.get("symbol", "") for c in cpc_classifications if c.get("symbol")
    )

    # --- Backward citations: patents this patent references ---
    # Path: biblio.references_cited.citations[i].patcit.lens_id
    # nplcit entries (journal papers) have no lens_id and are skipped.
    refs_cited = biblio.get("references_cited", {})
    cites_ids  = []
    if isinstance(refs_cited, dict):
        for c in refs_cited.get("citations", []):
            cid = c.get("patcit", {}).get("lens_id", "")
            if cid:
                cites_ids.append(cid)
    cites = "; ".join(cites_ids)

    # --- Forward citations: patents that cite this patent ---
    # Path: biblio.cited_by.patents[i].lens_id
    cited_by_block = biblio.get("cited_by", {})
    cited_by_ids   = []
    if isinstance(cited_by_block, dict):
        for p in cited_by_block.get("patents", []):
            cid = p.get("lens_id", "")
            if cid:
                cited_by_ids.append(cid)
    cited_by = "; ".join(cited_by_ids)

    # --- Publication year ---
    date_published   = raw.get("date_published", "")
    publication_year = date_published[:4] if date_published else ""

    return {
        "patent_id":        lens_id,
        "title":            title,
        "abstract":         abstract,
        "ipc_codes":        ipc_codes,
        "cpc_codes":        cpc_codes,
        "cites":            cites,
        "cited_by":         cited_by,
        "publication_year": publication_year,
    }
