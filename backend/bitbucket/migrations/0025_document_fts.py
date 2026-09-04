from django.db import migrations


CREATE_STATEMENTS = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS bitbucket_document_fts USING fts5(
        filename,
        relative_path,
        project,
        repository,
        added_by,
        latest_commit_author,
        latest_commit_message,
        extracted_text
    )
    """,
    """
    INSERT INTO bitbucket_document_fts(
        rowid, filename, relative_path, project, repository, added_by,
        latest_commit_author, latest_commit_message, extracted_text
    )
    SELECT document.id, document.filename, document.relative_path,
           repository.project, repository.slug, document.added_by,
           document.latest_commit_author, document.latest_commit_message,
           document.extracted_text
    FROM bitbucket_document AS document
    INNER JOIN bitbucket_repository AS repository ON repository.id = document.repository_id
    WHERE document.kind = 'pdf'
      AND NOT EXISTS (
          SELECT 1 FROM bitbucket_document_fts AS search WHERE search.rowid = document.id
      )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS bitbucket_document_fts_insert
    AFTER INSERT ON bitbucket_document
    WHEN new.kind = 'pdf'
    BEGIN
        INSERT INTO bitbucket_document_fts(
            rowid, filename, relative_path, project, repository, added_by,
            latest_commit_author, latest_commit_message, extracted_text
        )
        SELECT new.id, new.filename, new.relative_path, repository.project,
               repository.slug, new.added_by, new.latest_commit_author,
               new.latest_commit_message, new.extracted_text
        FROM bitbucket_repository AS repository WHERE repository.id = new.repository_id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS bitbucket_document_fts_delete
    AFTER DELETE ON bitbucket_document
    WHEN old.kind = 'pdf'
    BEGIN
        DELETE FROM bitbucket_document_fts WHERE rowid = old.id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS bitbucket_document_fts_update
    AFTER UPDATE ON bitbucket_document
    BEGIN
        DELETE FROM bitbucket_document_fts WHERE rowid = old.id;
        INSERT INTO bitbucket_document_fts(
            rowid, filename, relative_path, project, repository, added_by,
            latest_commit_author, latest_commit_message, extracted_text
        )
        SELECT new.id, new.filename, new.relative_path, repository.project,
               repository.slug, new.added_by, new.latest_commit_author,
               new.latest_commit_message, new.extracted_text
        FROM bitbucket_repository AS repository
        WHERE repository.id = new.repository_id AND new.kind = 'pdf';
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS bitbucket_repository_fts_update
    AFTER UPDATE OF project, slug ON bitbucket_repository
    BEGIN
        UPDATE bitbucket_document_fts
        SET project = new.project, repository = new.slug
        WHERE rowid IN (
            SELECT id FROM bitbucket_document
            WHERE repository_id = new.id AND kind = 'pdf'
        );
    END
    """,
)

DROP_STATEMENTS = (
    "DROP TRIGGER IF EXISTS bitbucket_repository_fts_update",
    "DROP TRIGGER IF EXISTS bitbucket_document_fts_update",
    "DROP TRIGGER IF EXISTS bitbucket_document_fts_delete",
    "DROP TRIGGER IF EXISTS bitbucket_document_fts_insert",
    "DROP TABLE IF EXISTS bitbucket_document_fts",
)


def create_document_fts(_apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return
    for statement in CREATE_STATEMENTS:
        schema_editor.execute(statement)


def drop_document_fts(_apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return
    for statement in DROP_STATEMENTS:
        schema_editor.execute(statement)


class Migration(migrations.Migration):
    dependencies = [("bitbucket", "0024_pdf_content_index")]

    operations = [migrations.RunPython(create_document_fts, drop_document_fts)]
