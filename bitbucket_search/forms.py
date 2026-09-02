"""Forms for secure, host-scoped Bitbucket HTTPS credentials."""

from __future__ import annotations

from django import forms

from bitbucket_search.models import BitbucketHTTPSCredentialKind


class BitbucketHTTPSCredentialForm(forms.Form):
    """Accept a new token without ever rendering a previously stored token."""

    origin = forms.ChoiceField(label="Repository host")
    credential_kind = forms.ChoiceField(
        label="Credential type",
        choices=(
            (
                BitbucketHTTPSCredentialKind.CLOUD_API_TOKEN,
                "Atlassian API token (recommended for Bitbucket Cloud)",
            ),
            (
                BitbucketHTTPSCredentialKind.CLOUD_ACCESS_TOKEN,
                "Repository, project, or workspace access token (Bitbucket Cloud)",
            ),
            (
                BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
                "Account name + HTTP access token (Bitbucket Data Center)",
            ),
        ),
    )
    account_name = forms.CharField(
        label="Bitbucket account name",
        max_length=255,
        required=False,
        strip=True,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "autocapitalize": "none",
                "spellcheck": "false",
                "placeholder": "Required only for account name + token",
            }
        ),
    )
    token = forms.CharField(
        label="HTTPS token",
        max_length=16_384,
        strip=False,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={
                "autocomplete": "new-password",
                "autocapitalize": "none",
                "spellcheck": "false",
                "data-bitbucket-token-input": "",
            },
        ),
    )

    def __init__(self, *args, origin_choices=(), **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["origin"].choices = tuple((origin, origin) for origin in origin_choices)
        self.fields["token"].help_text = (
            "OWL encrypts this token locally and never writes it into a repository URL. "
            "Saving a new value replaces the token for this exact host."
        )

    def clean_account_name(self) -> str:
        account_name = self.cleaned_data.get("account_name", "")
        if any(ord(character) < 32 for character in account_name):
            raise forms.ValidationError(
                "Enter an account name without control characters.", code="invalid_account_name"
            )
        return account_name

    def clean_token(self) -> str:
        token = self.cleaned_data.get("token", "")
        if token != token.strip():
            raise forms.ValidationError(
                "Remove spaces before or after the token.", code="ambiguous_token"
            )
        if any(ord(character) < 32 for character in token):
            raise forms.ValidationError(
                "Enter a token without control characters.", code="invalid_token"
            )
        return token

    def clean(self):
        cleaned_data = super().clean()
        kind = cleaned_data.get("credential_kind")
        origin = cleaned_data.get("origin", "")
        account_name = cleaned_data.get("account_name", "")
        if kind == BitbucketHTTPSCredentialKind.USERNAME_TOKEN and not account_name:
            self.add_error("account_name", "Enter the Bitbucket account name used with this token.")
        if kind in {
            BitbucketHTTPSCredentialKind.CLOUD_API_TOKEN,
            BitbucketHTTPSCredentialKind.CLOUD_ACCESS_TOKEN,
        } and origin not in {"https://bitbucket.org", "https://bitbucket.org:443"}:
            self.add_error(
                "credential_kind",
                "Bitbucket Cloud token types can be saved only for bitbucket.org. "
                "Choose account name + token for a Bitbucket Data Center host.",
            )
        return cleaned_data
