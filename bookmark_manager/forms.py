from __future__ import annotations

from django import forms
from django.conf import settings
from django.db import OperationalError, ProgrammingError

from bookmark_manager.models import BookmarkAvailability, Tag
from bookmark_manager.services.confluence_validation import (
    OriginValidationError,
    validate_confluence_origin,
)


class ConfluenceSettingsForm(forms.Form):
    base_url = forms.CharField(
        label="Confluence base URL",
        max_length=2048,
        widget=forms.URLInput(
            attrs={
                "autocomplete": "url",
                "inputmode": "url",
                "placeholder": "https://confluence.example.invalid/wiki",
            }
        ),
    )
    personal_access_token = forms.CharField(
        label="Personal Access Token",
        max_length=16_384,
        required=False,
        strip=True,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={
                "autocomplete": "new-password",
                "autocapitalize": "none",
                "spellcheck": "false",
                "data-pat-input": "",
            },
        ),
    )
    auth_mode = forms.ChoiceField(
        label="Authentication mode",
        choices=(("bearer", "Bearer token"),),
        initial="bearer",
    )
    verification_receipt = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(
        self,
        *args,
        current_base_url: str = "",
        has_stored_credential: bool = False,
        managed_externally: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.current_base_url = current_base_url
        self.has_stored_credential = has_stored_credential
        self.managed_externally = managed_externally
        self.cleaned_origin = None
        if has_stored_credential:
            self.fields["personal_access_token"].label = "Replace PAT"
            self.fields[
                "personal_access_token"
            ].help_text = "Stored securely. Leave this empty to keep the current PAT."
        else:
            self.fields["personal_access_token"].help_text = (
                "The PAT is sent only in this CSRF-protected request and stored in the "
                "operating-system credential store."
            )
        if managed_externally:
            for field_name in ("base_url", "personal_access_token", "auth_mode"):
                self.fields[field_name].disabled = True
                self.fields[field_name].required = False
            self.fields["base_url"].widget.attrs["placeholder"] = "Managed outside OWL"
            self.fields["personal_access_token"].widget.attrs["placeholder"] = "Managed outside OWL"

    def clean_base_url(self) -> str:
        if self.managed_externally:
            return ""
        try:
            self.cleaned_origin = validate_confluence_origin(
                self.cleaned_data["base_url"],
                allow_test_targets=settings.OWL_ALLOW_SYNTHETIC_CONFLUENCE_TARGETS,
            )
        except OriginValidationError as exc:
            raise forms.ValidationError(str(exc), code=exc.code) from exc
        return self.cleaned_origin.base_url

    def clean_personal_access_token(self) -> str:
        value = self.cleaned_data.get("personal_access_token", "").strip()
        if value and any(ord(character) < 32 for character in value):
            raise forms.ValidationError(
                "Enter a PAT without control characters.", code="invalid_pat"
            )
        return value

    def clean(self):
        cleaned_data = super().clean()
        if self.managed_externally or self.errors:
            return cleaned_data
        base_url = cleaned_data.get("base_url", "")
        token = cleaned_data.get("personal_access_token", "")
        if not self.has_stored_credential and not token:
            self.add_error("personal_access_token", "Enter a PAT for this Confluence profile.")
        elif self.current_base_url and base_url != self.current_base_url and not token:
            self.add_error(
                "personal_access_token",
                "Enter a new PAT when changing the Confluence origin.",
            )
        return cleaned_data


class BookmarkInputForm(forms.Form):
    page = forms.CharField(
        label="Bookmark URL or Confluence Page ID",
        max_length=4096,
        strip=True,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "placeholder": "Paste any URL or a Confluence Page ID",
            }
        ),
    )


class BookmarkFilterForm(forms.Form):
    """Validated local-only bookmark search, filter, and sort controls."""

    SORT_CHOICES = (
        ("added_newest", "Added newest"),
        ("added_oldest", "Added oldest"),
        ("updated_newest", "Updated newest"),
        ("updated_oldest", "Updated oldest"),
        ("created_newest", "Created newest"),
        ("created_oldest", "Created oldest"),
        ("title_ascending", "Title A–Z"),
        ("title_descending", "Title Z–A"),
        ("author_ascending", "Author A–Z"),
        ("favorites_first", "Favorites first"),
        ("pinned_first", "Pinned first"),
        ("most_opened", "Most opened"),
        ("least_opened", "Least opened"),
        ("recently_opened", "Recently opened"),
        ("least_recently_opened", "Least recently opened"),
        ("recently_refreshed", "Recently refreshed"),
    )
    DATE_FIELD_CHOICES = (
        ("", "Choose a date"),
        ("created", "Created in Confluence"),
        ("updated", "Updated in Confluence"),
        ("added", "Added to OWL"),
        ("refreshed", "Last refreshed"),
        ("viewed", "Last viewed"),
    )
    DATE_PRESET_CHOICES = (
        ("any_time", "Any time"),
        ("today", "Today"),
        ("last_7_days", "Last 7 days"),
        ("last_30_days", "Last 30 days"),
        ("last_3_months", "Last 3 months"),
        ("last_6_months", "Last 6 months"),
        ("this_year", "This year"),
        ("last_year", "Last year"),
        ("older", "Older"),
        ("custom_range", "Custom range"),
    )

    q = forms.CharField(
        max_length=500,
        required=False,
        strip=True,
        widget=forms.SearchInput(
            attrs={
                "autocomplete": "off",
                "placeholder": "Search bookmarks, or paste any URL",
                "data-bookmark-search": "",
            }
        ),
    )
    category = forms.IntegerField(required=False, min_value=1, widget=forms.HiddenInput())
    favorite = forms.BooleanField(required=False)
    pinned = forms.BooleanField(required=False)
    tags = forms.MultipleChoiceField(required=False)
    person = forms.CharField(max_length=500, required=False, strip=True)
    space = forms.CharField(max_length=255, required=False, strip=True)
    availability = forms.MultipleChoiceField(
        required=False,
        choices=BookmarkAvailability.choices,
    )
    recency = forms.MultipleChoiceField(
        required=False,
        choices=(
            ("new", "New"),
            ("updated", "Updated"),
            ("normal", "Normal"),
        ),
    )
    changed = forms.BooleanField(required=False)
    broken = forms.BooleanField(required=False)
    date_field = forms.ChoiceField(required=False, choices=DATE_FIELD_CHOICES)
    date_preset = forms.ChoiceField(required=False, choices=DATE_PRESET_CHOICES)
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    min_open = forms.IntegerField(required=False, min_value=0)
    max_open = forms.IntegerField(required=False, min_value=0)
    sort = forms.ChoiceField(required=False, choices=SORT_CHOICES, initial="added_newest")

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs["form"] = "bookmark-filter-form"
            field.widget.attrs["id"] = f"bookmark-filter-{name.replace('_', '-')}"
        try:
            self.fields["tags"].choices = tuple(
                Tag.objects.order_by("normalized_name").values_list("normalized_name", "name")
            )
        except (OperationalError, ProgrammingError):
            self.fields["tags"].choices = ()

    def clean(self):
        cleaned_data = super().clean()
        minimum = cleaned_data.get("min_open")
        maximum = cleaned_data.get("max_open")
        if minimum is not None and maximum is not None and minimum > maximum:
            self.add_error("max_open", "Maximum opens must be at least the minimum.")
        date_from = cleaned_data.get("date_from")
        date_to = cleaned_data.get("date_to")
        if date_from and date_to and date_from > date_to:
            self.add_error("date_to", "End date must not be before the start date.")
        if cleaned_data.get("date_preset") == "custom_range" and not (date_from or date_to):
            self.add_error("date_from", "Choose a start date, an end date, or both.")
        return cleaned_data


class BookmarkOrganisationForm(forms.Form):
    notes = forms.CharField(
        required=False,
        max_length=50_000,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 5, "id": "bookmark-organisation-notes"}),
    )
    tags = forms.CharField(
        required=False,
        max_length=2_000,
        strip=True,
        widget=forms.TextInput(
            attrs={
                "id": "bookmark-organisation-tags",
                "placeholder": "network, architecture, review",
            }
        ),
    )


class BookmarkCategoryRenameForm(forms.Form):
    name = forms.CharField(max_length=253, strip=True)


class SavedBookmarkViewForm(forms.Form):
    name = forms.CharField(
        max_length=120,
        strip=True,
        widget=forms.TextInput(attrs={"id": "saved-bookmark-view-name"}),
    )


class BookmarkImportForm(forms.Form):
    import_file = forms.FileField(
        label="JSON backup or legacy bookmark file",
        help_text="Choose a UTF-8 JSON file. Valid records continue if another record fails.",
        widget=forms.ClearableFileInput(
            attrs={"accept": ".json,application/json", "id": "bookmark-import-file"}
        ),
    )

    def clean_import_file(self):
        uploaded = self.cleaned_data["import_file"]
        maximum = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        if uploaded.size > maximum:
            raise forms.ValidationError(
                f"The import is larger than the configured {maximum:,}-byte limit."
            )
        if not uploaded.name.casefold().endswith(".json"):
            raise forms.ValidationError("Choose a .json file.")
        return uploaded
