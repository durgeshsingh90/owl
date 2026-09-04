from django import forms

from bitbucket.services.repository_urls import (
    ProjectURL,
    ProjectURLValidationError,
    RepositoryURL,
    RepositoryURLValidationError,
    ServerURL,
    ServerURLValidationError,
    parse_api_base_url,
    parse_project_url,
    parse_repository_url,
)


class RepositoryForm(forms.Form):
    repository_url = forms.CharField(
        max_length=2048,
        label="HTTPS repository URL",
        widget=forms.URLInput(
            attrs={
                "placeholder": "https://server.example/scm/project/repository.git",
                "autocomplete": "off",
                "spellcheck": "false",
            }
        ),
    )
    username = forms.CharField(
        max_length=255,
        required=False,
        label="Bitbucket username",
        widget=forms.TextInput(
            attrs={
                "autocomplete": "username",
                "spellcheck": "false",
            }
        ),
    )
    access_token = forms.CharField(
        max_length=16_384,
        required=False,
        label="HTTP access token",
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "new-password",
                "spellcheck": "false",
            },
            render_value=False,
        ),
    )

    parsed_url: RepositoryURL | None = None

    def clean_repository_url(self) -> str:
        try:
            self.parsed_url = parse_repository_url(self.cleaned_data["repository_url"])
        except RepositoryURLValidationError as exc:
            raise forms.ValidationError(str(exc), code="invalid_repository_url") from exc
        return self.parsed_url.url


class ServerSettingsForm(forms.Form):
    base_url = forms.CharField(max_length=2048, label="Bitbucket REST API base URL")
    username = forms.CharField(max_length=255, required=False, label="Bitbucket username")
    access_token = forms.CharField(max_length=16_384, required=False, label="HTTP access token")
    verify_ssl = forms.BooleanField(required=False, initial=True, label="Verify SSL certificates")

    parsed_url: ServerURL | None = None

    def clean_base_url(self) -> str:
        try:
            self.parsed_url = parse_api_base_url(self.cleaned_data["base_url"])
        except ServerURLValidationError as exc:
            raise forms.ValidationError(str(exc), code="invalid_api_base_url") from exc
        return self.parsed_url.api_base_url


class SourceForm(forms.Form):
    source_type = forms.ChoiceField(choices=(("project", "Project"), ("repository", "Repository")))
    source_url = forms.CharField(max_length=2048)

    parsed_project: ProjectURL | None = None
    parsed_repository: RepositoryURL | None = None

    def clean_source_url(self) -> str:
        value = self.cleaned_data["source_url"]
        source_type = self.data.get("source_type", "")
        try:
            if source_type == "project":
                self.parsed_project = parse_project_url(value)
                return self.parsed_project.url
            self.parsed_repository = parse_repository_url(value)
            return self.parsed_repository.url
        except (ProjectURLValidationError, RepositoryURLValidationError) as exc:
            raise forms.ValidationError(str(exc), code="invalid_source_url") from exc
