import datetime as dt
import string

import factory

from procare_photo_downloader.config import Credentials, Token
from procare_photo_downloader.models import Kid, MediaItem, MediaKind


class KidFactory(factory.Factory[Kid]):
    class Meta:  # ty: ignore[incompatible-attribute-override]
        model = Kid

    id = factory.Faker("uuid4")
    name = factory.Faker("name")
    created_at = factory.LazyFunction(lambda: dt.datetime.now(dt.UTC))


class TokenFactory(factory.Factory[Token]):
    class Meta:  # ty: ignore[incompatible-attribute-override]
        model = Token

    auth_token = factory.Faker(
        "lexify",
        text="?" * 50,
        letters=string.ascii_letters + string.digits + "_",
    )
    email = factory.Faker("email")


class CredentialsFactory(factory.Factory[Credentials]):
    class Meta:  # ty: ignore[incompatible-attribute-override]
        model = Credentials

    email = factory.Faker("email")
    password = factory.Faker(
        "lexify",
        text="?" * 20,
        letters=string.ascii_letters + string.digits,
    )


class MediaItemFactory(factory.Factory[MediaItem]):
    class Meta:  # ty: ignore[incompatible-attribute-override]
        model = MediaItem

    id = factory.Faker("uuid4")
    kid = factory.SubFactory(KidFactory)
    kid_id = factory.SelfAttribute("kid.id")
    kind = MediaKind.PHOTO
    captured_at = factory.LazyFunction(lambda: dt.datetime.now(dt.UTC))
    url = factory.LazyAttribute(lambda obj: f"https://media.example.com/photos/{obj.id}.jpg")
    caption = ""
