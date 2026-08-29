from django.db import migrations

CREATE_METADATA_FTS = (
    """
    CREATE VIRTUAL TABLE bitbucket_search_pdf_metadata_fts USING fts5(
        document_id UNINDEXED,
        filename,
        relative_path,
        repository_name,
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
    """
    INSERT INTO bitbucket_search_pdf_metadata_fts(
        document_id,
        filename,
        relative_path,
        repository_name
    )
    SELECT
        document.id,
        document.filename,
        document.relative_path,
        repository.display_name
    FROM bitbucket_search_pdfdocument AS document
    JOIN bitbucket_search_bitbucketrepository AS repository
      ON repository.id = document.repository_id
    """,
    """
    CREATE TRIGGER bitbucket_search_pdf_metadata_fts_insert
    AFTER INSERT ON bitbucket_search_pdfdocument BEGIN
        INSERT INTO bitbucket_search_pdf_metadata_fts(
            document_id,
            filename,
            relative_path,
            repository_name
        )
        SELECT
            new.id,
            new.filename,
            new.relative_path,
            repository.display_name
        FROM bitbucket_search_bitbucketrepository AS repository
        WHERE repository.id = new.repository_id;
    END
    """,
    """
    CREATE TRIGGER bitbucket_search_pdf_metadata_fts_delete
    AFTER DELETE ON bitbucket_search_pdfdocument BEGIN
        DELETE FROM bitbucket_search_pdf_metadata_fts
        WHERE document_id = old.id;
    END
    """,
    """
    CREATE TRIGGER bitbucket_search_pdf_metadata_fts_update
    AFTER UPDATE OF filename, relative_path, repository_id
    ON bitbucket_search_pdfdocument BEGIN
        DELETE FROM bitbucket_search_pdf_metadata_fts
        WHERE document_id = old.id;
        INSERT INTO bitbucket_search_pdf_metadata_fts(
            document_id,
            filename,
            relative_path,
            repository_name
        )
        SELECT
            new.id,
            new.filename,
            new.relative_path,
            repository.display_name
        FROM bitbucket_search_bitbucketrepository AS repository
        WHERE repository.id = new.repository_id;
    END
    """,
    """
    CREATE TRIGGER bitbucket_search_pdf_repository_fts_update
    AFTER UPDATE OF display_name ON bitbucket_search_bitbucketrepository BEGIN
        DELETE FROM bitbucket_search_pdf_metadata_fts
        WHERE document_id IN (
            SELECT id
            FROM bitbucket_search_pdfdocument
            WHERE repository_id = new.id
        );
        INSERT INTO bitbucket_search_pdf_metadata_fts(
            document_id,
            filename,
            relative_path,
            repository_name
        )
        SELECT
            document.id,
            document.filename,
            document.relative_path,
            new.display_name
        FROM bitbucket_search_pdfdocument AS document
        WHERE document.repository_id = new.id;
    END
    """,
)

CREATE_PAGE_FTS = (
    """
    CREATE VIRTUAL TABLE bitbucket_search_pdf_page_fts USING fts5(
        extracted_text,
        content='bitbucket_search_pdftextpage',
        content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TRIGGER bitbucket_search_pdf_page_fts_insert
    AFTER INSERT ON bitbucket_search_pdftextpage BEGIN
        INSERT INTO bitbucket_search_pdf_page_fts(rowid, extracted_text)
        VALUES (new.id, new.extracted_text);
    END
    """,
    """
    CREATE TRIGGER bitbucket_search_pdf_page_fts_delete
    AFTER DELETE ON bitbucket_search_pdftextpage BEGIN
        INSERT INTO bitbucket_search_pdf_page_fts(
            bitbucket_search_pdf_page_fts,
            rowid,
            extracted_text
        ) VALUES ('delete', old.id, old.extracted_text);
    END
    """,
    """
    CREATE TRIGGER bitbucket_search_pdf_page_fts_update
    AFTER UPDATE OF extracted_text ON bitbucket_search_pdftextpage BEGIN
        INSERT INTO bitbucket_search_pdf_page_fts(
            bitbucket_search_pdf_page_fts,
            rowid,
            extracted_text
        ) VALUES ('delete', old.id, old.extracted_text);
        INSERT INTO bitbucket_search_pdf_page_fts(rowid, extracted_text)
        VALUES (new.id, new.extracted_text);
    END
    """,
    """
    INSERT INTO bitbucket_search_pdf_page_fts(bitbucket_search_pdf_page_fts)
    VALUES ('rebuild')
    """,
)

DROP_PAGE_FTS = (
    "DROP TRIGGER IF EXISTS bitbucket_search_pdf_page_fts_update",
    "DROP TRIGGER IF EXISTS bitbucket_search_pdf_page_fts_delete",
    "DROP TRIGGER IF EXISTS bitbucket_search_pdf_page_fts_insert",
    "DROP TABLE IF EXISTS bitbucket_search_pdf_page_fts",
)

DROP_METADATA_FTS = (
    "DROP TRIGGER IF EXISTS bitbucket_search_pdf_repository_fts_update",
    "DROP TRIGGER IF EXISTS bitbucket_search_pdf_metadata_fts_update",
    "DROP TRIGGER IF EXISTS bitbucket_search_pdf_metadata_fts_delete",
    "DROP TRIGGER IF EXISTS bitbucket_search_pdf_metadata_fts_insert",
    "DROP TABLE IF EXISTS bitbucket_search_pdf_metadata_fts",
)


class Migration(migrations.Migration):
    dependencies = [
        ("bitbucket_search", "0003_pdf_extraction_and_search"),
    ]

    operations = [
        migrations.RunSQL(CREATE_METADATA_FTS, DROP_METADATA_FTS),
        migrations.RunSQL(CREATE_PAGE_FTS, DROP_PAGE_FTS),
    ]
