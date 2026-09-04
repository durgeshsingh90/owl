from django import forms

from bitbucket.services.repository_urls import (
    RepositoryURL,
    RepositoryURLValidationError,
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

    parsed_url: RepositoryURL | None = None

    def clean_repository_url(self) -> str:
        try:
            self.parsed_url = parse_repository_url(self.cleaned_data["repository_url"])
        except RepositoryURLValidationError as exc:
            raise forms.ValidationError(str(exc), code="invalid_repository_url") from exc
        return self.parsed_url.url
