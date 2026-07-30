from __future__ import annotations

import math
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings


@dataclass(frozen=True)
class S3UploadConfig:
    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str
    region: str
    addressing_style: str
    signature_version: str
    part_size: int
    expires_in: int


def get_s3_upload_config() -> S3UploadConfig | None:
    endpoint_url = getattr(settings, "S3_UPLOAD_ENDPOINT_URL", "")
    access_key = getattr(settings, "S3_UPLOAD_ACCESS_KEY", "")
    secret_key = getattr(settings, "S3_UPLOAD_SECRET_KEY", "")
    bucket = getattr(settings, "S3_UPLOAD_BUCKET", "")

    if not endpoint_url or not access_key or not secret_key or not bucket:
        return None

    return S3UploadConfig(
        endpoint_url=endpoint_url,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        region=getattr(settings, "S3_UPLOAD_REGION", "us-east-1"),
        addressing_style=getattr(settings, "S3_UPLOAD_ADDRESSING_STYLE", "path"),
        signature_version=getattr(settings, "S3_UPLOAD_SIGNATURE_VERSION", "s3v4"),
        part_size=int(getattr(settings, "S3_UPLOAD_PART_SIZE", 16 * 1024 * 1024)),
        expires_in=int(getattr(settings, "S3_UPLOAD_PART_URL_EXPIRES", 3600)),
    )


def build_object_key(os_id: int, filename: str) -> str:
    safe_name = Path(filename).name.replace(" ", "_")
    return f"isos/{os_id}/{uuid.uuid4().hex}-{safe_name}"


def total_parts_for_size(file_size: int, part_size: int) -> int:
    if file_size <= 0:
        return 1
    return int(math.ceil(file_size / float(part_size)))


def s3_client(config: S3UploadConfig):
    import boto3
    from botocore.client import Config

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        region_name=config.region,
        config=Config(
            signature_version=config.signature_version,
            s3={"addressing_style": config.addressing_style},
        ),
    )


def enabled() -> bool:
    return get_s3_upload_config() is not None
