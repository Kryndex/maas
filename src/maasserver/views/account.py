# Copyright 2012-2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Account views."""

__all__ = [
    "authenticate",
    "login",
    "logout",
    ]

from django import forms
from django.conf import settings as django_settings
from django.contrib.auth import (
    authenticate as dj_authenticate,
    REDIRECT_FIELD_NAME,
)
from django.contrib.auth.views import (
    login as dj_login,
    logout as dj_logout,
)
from django.http import (
    HttpResponseForbidden,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import render
from maasserver.models import UserProfile
from maasserver.models.user import (
    create_auth_token,
    get_auth_tokens,
)
from maasserver.utils.django_urls import reverse


def login(request):
    extra_context = {
        'no_users': UserProfile.objects.all_users().count() == 0,
        'create_command': django_settings.MAAS_CLI,
        }
    if request.user.is_authenticated:
        return HttpResponseRedirect(reverse('index'))
    else:
        redirect_url = request.GET.get(
            REDIRECT_FIELD_NAME, request.POST.get(REDIRECT_FIELD_NAME))
        if redirect_url == reverse('logout'):
            redirect_field_name = None  # Ignore next page.
        else:
            redirect_field_name = REDIRECT_FIELD_NAME
        return dj_login(
            request, redirect_field_name=redirect_field_name,
            extra_context=extra_context)


class LogoutForm(forms.Form):
    """Log-out confirmation form.

    There is nothing interesting in this form, but it's needed in order
    to get Django's CSRF protection during logout.
    """


def logout(request):
    if request.method == 'POST':
        form = LogoutForm(request.POST)
        if form.is_valid():
            return dj_logout(request, next_page=reverse('login'))
    else:
        form = LogoutForm()

    return render(
        request, 'maasserver/logout_confirm.html', {'form': form})


def authenticate(request):
    """Authenticate a user, but do *not* log them in.

    If the correct username and password are given, credentials suitable for
    use with MAAS's Web API are returned. This can be used by client libraries
    to exchange a username+password for an API token.

    Accepts HTTP POST requests with the following parameters:

      username: The user's username.
      password: The user's password.
      consumer: The name to use for the token, which can be used to later
          understand which consumer requested and is using a token. Optional.

    If `consumer` is provided, existing credentials belonging to the user with
    a matching consumer will be returned, if any exist, else new credentials
    will be created and labelled with `consumer` before being returned.

    If `consumer` is not provided, the earliest created credentials belonging
    to the user will be returned. If no preexisting credentials exist, new
    credentials will be created and returned.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    username = request.POST.get("username")
    password = request.POST.get("password")
    consumer = request.POST.get("consumer")
    user = dj_authenticate(username=username, password=password)

    if user is None or not user.is_active:
        # This is_active check mimics confirm_login_allowed from Django's
        # django.contrib.auth.forms.AuthenticationForm.
        return HttpResponseForbidden()

    # Find an existing token. There might be more than one so take the first.
    tokens = get_auth_tokens(user)
    if consumer is not None:
        tokens = tokens.filter(consumer__name=consumer)
    token = tokens.first()

    # When no existing token is found, create a new one.
    if token is None:
        token = create_auth_token(user, consumer)

    # Return something with the same shape as that rendered by
    # AccountHandler.create_authorisation_token.
    return JsonResponse({
        "consumer_key": token.consumer.key,
        "name": token.consumer.name,
        "token_key": token.key,
        "token_secret": token.secret,
    })
