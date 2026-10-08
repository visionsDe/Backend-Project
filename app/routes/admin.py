# Standard library
import csv
import io
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Union

logger = logging.getLogger(__name__)

# Third-party
import pandas as pd
import stripe
from jose import JWTError
from pydantic import EmailStr

# FastAPI
from fastapi import (
    APIRouter, BackgroundTasks, Cookie, Depends, File, Form,
    HTTPException, Query, Response, UploadFile, status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import OAuth2PasswordBearer

# SQLAlchemy
from sqlalchemy import and_, asc, cast, Date, desc, func, literal, or_
from sqlalchemy.orm import Session, aliased, joinedload
from sqlalchemy.sql import extract, case
from sqlalchemy.sql.functions import coalesce

# App
from app import models
from app.config import settings
from app.controller.base_controller import BaseController
from app.controller.pagination_controller import PaginationResponse
from app.database import get_db
from app.helpers.messages import messages
from app.models.common import EmailTemplate, EmailTemplateTranslation
from app.schemas import (
    admin, bookings as booking_model, chat as chat_model,
    common, suppliers, suppliers as supplier_model, users,
)
from app.schemas.common import (
    EmailTemplateCreate, EmailTemplateInDB,
    EmailTemplateUpdate, EmailTemplateTranslationInDB,
)
from app.utils.auth import admin_required
from app.utils.bookings import compute_booking_total
from app.utils.encryption import hash_password, verify_password, encrypt_data, decrypt_data, hash_string, hash_sort_string
from app.utils.email_utils import send_supplier_status_email, get_email_template_content, send_order_email, send_subscription_email, send_moderation_email
from app.utils.helper import set_language_cookie, merge_two_dicts, allowed_file, get_gender, convert_date, get_added_subscriptions, check_active_subscription, normalize_subscription_type, ModerationStatus, SubscriptionType, SUBSCRIPTION_PLAN_TYPES, SUBSCRIPTION_SLOTS, SUBSCRIPTION_PLAN_DEFAULTS
from app.utils.jwt_token import create_access_token, verify_access_token, email_token_verification
from app.utils.log_utils import add_approved_user_logs
from app.utils.org_membership import (
    count_active_members as _count_active_members,
    get_member_discount_matrix as _member_discount_matrix,
)
from app.utils.notification_utils import send_push_notification, get_notification_data
from app.utils.stripe_client import map_stripe_requirements_to_status, stripe_api_key_for


ICONS_DIR = "static/icons/languages"
CAT_ICONS_DIR = "static/icons/categories"
COUNTRY_UPLOAD_DIR = "static/icons/countries"
NATIONALITY_UPLOAD_DIR = "static/icons/nationality"

router = APIRouter(
    prefix="/admin",
    tags=["Admin"]
)
selected_lang = 'en'#settings.DEFAULT_LANGUAGE
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/admin/login")

# ──────────────────────────────────────────────
# Authentication
# ──────────────────────────────────────────────
@router.post("/login")
def login(
    response: Response,
    username: EmailStr = Form(...),
    password: str = Form(...),
    remember_token: Optional[bool] = Form(None),
    db: Session = Depends(get_db)
):
    try:
        db_user = db.query(models.User).filter(models.User.email == username, models.User.role_id == 1).first()
        if not db_user or not verify_password(password, db_user.password):
            return JSONResponse(content = {"message": "Invalid credentials"}, status_code=status.HTTP_401_UNAUTHORIZED)

        if not db_user.email_verified_at:
            return JSONResponse(content = {"message": messages[selected_lang]['email_not_verify']}, status_code=status.HTTP_401_UNAUTHORIZED)

        if db_user.status in ('Deleted', 'Rejected'):
            return JSONResponse(content = {"message": "Account is not active"}, status_code=status.HTTP_403_FORBIDDEN)

        # Rotate the session id on every login — kills any previously-issued admin token.
        new_session_id = str(uuid.uuid4())
        db_user.session_token_id = new_session_id

        access_token = create_access_token(data={"email": db_user.email, "status": db_user.status, "sid": new_session_id}, remember_me=remember_token)

        db_user.remember_token = remember_token
        db.commit()

        set_language_cookie(response, db_user.language.code)

        accessToken = {"access_token": access_token, "token_type": "bearer"}
        userInfo = {
            "first_name": decrypt_data(db_user.first_name),
            "last_name": decrypt_data(db_user.last_name),
            "email": db_user.email
        }
        responseData = merge_two_dicts(accessToken, userInfo)
        return BaseController.success(responseData,'')
    except JWTError:
        return BaseController.errorGeneral(messages[selected_lang]['something_wrong'], status.HTTP_404_NOT_FOUND)

    
# ──────────────────────────────────────────────
# Languages
# ──────────────────────────────────────────────
@router.put("/set_default_language/{language_id}", dependencies=[Depends(admin_required)])
def set_default_language(
    response: Response,
    language_id: int,
    db: Session = Depends(get_db),
    selected_language: Optional[str]=Cookie(default='en')):
    try:
        language = db.query(models.Language).filter(models.Language.id == language_id).first()
        if not language:
            return BaseController.errorGeneral(messages[selected_language]['language_not_support'], 400)
        
        db.query(models.Language).update({models.Language.default_language: False})
        db.query(models.Language).filter(models.Language.id == language_id).update({models.Language.default_language: True})
        db.commit()

        set_language_cookie(response, language.code)
            
        return BaseController.success([], messages[selected_lang]['language_updated'])
    except JWTError:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'])
    
    
@router.post("/update_language/{language_id}", dependencies=[Depends(admin_required)])
async def update_language(
    language_id: int,
    name: Optional[str] = None,
    code: Optional[str] = None,
    icon: UploadFile = File(None),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    language_info = db.query(models.Language).filter(models.Language.id == language_id).first()
    if not language_info:
        return BaseController.errorGeneral(messages[selected_language]['language_not_found'], status.HTTP_404_NOT_FOUND)

    if icon:
        if not icon.content_type.startswith("image/"):
            return BaseController.errorGeneral(
                messages[selected_language]['invalid_file_format'], 400)
        
        if not allowed_file(icon.filename):
            return BaseController.errorGeneral(messages[selected_language]['invalid_extension'], 400)
        
        if not os.path.exists(os.path.join(settings.FILE_DIR_PATH, ICONS_DIR)):
            os.makedirs(os.path.join(settings.FILE_DIR_PATH, ICONS_DIR))

        file_extension = icon.filename.split(".")[-1]
        file_name = f"{code}.{file_extension}" if code else f"{language_info.code}.{file_extension}"
        file_path = os.path.join(ICONS_DIR, file_name)

        with open(os.path.join(settings.FILE_DIR_PATH, file_path), "wb") as f:
            f.write(await icon.read())
        
        language_info.icon = file_path

    if name:
        language_info.name = name
    if code:
        language_info.code = code

    db.commit()

    return BaseController.success([], messages[selected_language]['language_updated'])


# ──────────────────────────────────────────────
# Organizations
# ──────────────────────────────────────────────
@router.get("/subscription-picker", dependencies=[Depends(admin_required)])
def get_subscription_picker():
    """Return the store-catalogue picker used by admin forms"""
    subscription_data = get_added_subscriptions() or {}
    return BaseController.success(
        {
            "subscription_plans_picker": _group_subscriptions_by_plan(subscription_data),
        },
        "Subscription picker",
    )


@router.get("/organizations", dependencies=[Depends(admin_required)])
def get_organizations_list(
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    language_id = (
            db.query(models.Language.id)
            .filter(models.Language.code == selected_language)
            .scalar() or 1
        )

    organizations = db.query(models.Organization).all()

    subscription_data = get_added_subscriptions() or {}
    subscription_plans_picker = _group_subscriptions_by_plan(subscription_data)

    org_list = []
    for organization in organizations:
        default_lang_subject = db.query(models.OrganizationTranslation).filter(models.OrganizationTranslation.organization_id == organization.id, models.OrganizationTranslation.language_id == language_id).first()

        org_list.append({
            'id': organization.id,
            'slug': organization.slug,
            'name': default_lang_subject.org_name if default_lang_subject else None,
            'is_active': organization.is_active,
            'country_id': organization.country_id,
            'country_name': organization.country.country_name,
            'created_at': jsonable_encoder(organization.created_at),
            'has_document': organization.has_document,
            'responsible_person_name': organization.responsible_person_name,
            'responsible_person_email': organization.responsible_person_email,
            'free_trial_days': organization.free_trial_days,
            'member_discounts': _member_discount_matrix(db, organization.id),
            'active_member_count': _count_active_members(db, organization.id),
            'subscription_plans_picker': subscription_plans_picker,
        })

    return BaseController.success(org_list, "Organization list") if org_list else BaseController.success([], "No organizations found.")

@router.get("/org_translation/{org_id}", dependencies=[Depends(admin_required)])
def get_organizations_list(
    org_id: int,
    db: Session = Depends(get_db),
):
    orgTransAlias = models.OrganizationTranslation
    langDetailAlias = models.Language
    org_translations = db.query(orgTransAlias, langDetailAlias).outerjoin(langDetailAlias, langDetailAlias.id == orgTransAlias.language_id).filter(
            models.OrganizationTranslation.organization_id == org_id
        ).all()
    translations = []
    for orgTrans, langDetail in org_translations:
            translations.append({
                'id': orgTrans.id,
                'org_name': orgTrans.org_name,
                'language_detail': {'id':langDetail.id,
                                     'name':langDetail.name,
                                     'code':langDetail.code,
                                     'icon':langDetail.icon,
                                     } if langDetail else None
            })

    return BaseController.success(translations, "Organization translation list") if translations else BaseController.success([], "Translation not found.")

# ──────────────────────────────────────────────
# Currencies
# ──────────────────────────────────────────────
@router.get("/currencies", dependencies=[Depends(admin_required)])
def get_currencies_list(
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    currencies = db.query(models.Currency).options(joinedload(models.Currency.country)).all()

    response_list = []
    for currency in currencies:
        response_list.append(
            common.GetAdminCurrencies(
                id=currency.id,
                name=currency.name,
                code=currency.code,
                status=currency.status,
                symbol=currency.symbol,
                country_id=currency.country_id,
                country=currency.country.country_name if currency.country else None,
                created_at=currency.created_at,
                updated_at=currency.updated_at
            )
        )
    
    if not currencies:
        return BaseController.errorGeneral(messages[selected_language]['currency_not_found'], status.HTTP_404_NOT_FOUND)
    
    return BaseController.success(jsonable_encoder(response_list), messages[selected_language]['currency_list'])

@router.post("/add_currency", dependencies=[Depends(admin_required)])
def add_currency(form_data:common.addCurrency,
                 db: Session = Depends(get_db),
                 selected_language: Optional[str] = Cookie(default='en')
                 ):
    exist_currency = db.query(models.Currency).filter(models.Currency.code == form_data.code).first()
    if not exist_currency:
        new_currency = models.Currency(name=form_data.name, code=form_data.code)
        db.add(new_currency)
        db.commit()
        db.refresh(new_currency)
        return BaseController.success([], messages[selected_lang]['currency_added'])
    else:
        return BaseController.errorGeneral(messages[selected_language]["currency_exist"])
    
@router.put("/update_currency/{currency_id}", dependencies=[Depends(admin_required)])
def update_currency(response: Response,
    currency_id: int,
    form_data: common.updateCurrency,
    db: Session = Depends(get_db),
    selected_language: Optional[str]=Cookie(default='en')):
    try:
        currency = db.query(models.Currency).filter(models.Currency.id == currency_id).first()
        if not currency:
            return BaseController.errorGeneral(messages[selected_language]['currency_not_support'], 400)

        db.query(models.Currency).filter(models.Currency.id == currency_id).update({models.Currency.name: form_data.name, models.Currency.code: form_data.code, models.Currency.status: form_data.status, models.Currency.symbol: form_data.symbol})
        db.commit()

        return BaseController.success([],messages[selected_lang]['currency_updated'])
    except JWTError:
        return BaseController.errorGeneral(messages[selected_language]['something_went_wrong'])
    
# ──────────────────────────────────────────────
# Users
# ──────────────────────────────────────────────
@router.put("/update_user/{user_id}", dependencies=[Depends(admin_required)])
def update_user(user_id: int,
                form_data: users.updateStatus,
                db: Session = Depends(get_db),
                selected_language: Optional[str]=Cookie(default='en')):
    try:
        exist_user = db.query(models.User).filter(models.User.id == user_id).first()
        if not exist_user:
            return BaseController.errorGeneral(messages[selected_language]['user_not_found'], 400)
        
        db.query(models.User).filter(models.User.id == user_id).update({models.User.status: form_data.status, models.User.status_reason: form_data.status_reason})
        db.commit()

        return BaseController.success([],messages[selected_lang]['user_detail_updated'])
    except JWTError:
        return BaseController.errorGeneral(messages[selected_language]['something_went_wrong'])

@router.get("/language_list", dependencies=[Depends(admin_required)])
def get_language_list(db: Session = Depends(get_db), selected_language: Optional[str]=Cookie(default='en')):
    language_list = db.query(models.Language).all()   
    
    if language_list:
        return BaseController.success(jsonable_encoder([common.GetAllLanguages.model_validate(language) for language in language_list]), messages[selected_language]['language_list'])
    else:
        return BaseController.errorGeneral(messages[selected_language]['language_not_found'], 400)

@router.post("/add-language", dependencies=[Depends(admin_required)])
async def add_language(name: str, code: str, icon: UploadFile = File(...), db: Session = Depends(get_db), selected_language: Optional[str]=Cookie(default='en')):
    if not icon.content_type.startswith("image/"):
        return BaseController.errorGeneral(
                messages[selected_language]['invalid_file_format'], 400)
    
    # Validate file extension
    if not allowed_file(icon.filename):
        return BaseController.errorGeneral(messages[selected_language]['invalid_extension'], 400)
    
    # Create directory if not exists
    if not os.path.exists(os.path.join(settings.FILE_DIR_PATH, ICONS_DIR)):
        os.makedirs(os.path.join(settings.FILE_DIR_PATH, ICONS_DIR))

    # Define the file path and save the file
    file_extension = icon.filename.split(".")[-1]
    file_name = f"{code}.{file_extension}"
    file_path = os.path.join(ICONS_DIR, file_name)

    with open(os.path.join(settings.FILE_DIR_PATH, file_path), "wb") as f:
        f.write(await icon.read())

    language_info = models.Language(
        name=name,
        code=code,
        icon=file_path
    )

    db.add(language_info)
    db.commit()

    return BaseController.success([], messages[selected_language]['language_info'])   

@router.get("/user_list", dependencies=[Depends(admin_required)])
def get_user_list(db: Session = Depends(get_db),
                user_status: Optional[str] = Query(default=None, description="Status"),
                supplier_status: Optional[str] = Query(default=None, description="approved / rejected/ pending approval"),
                supplier_stripe_status: Optional[str] = Query(default=None, description="verified / unverified/ pending"),
                device_type: Optional[str] = Query(default=None, description="iOS / Android"),
                gender: Optional[str] = Query(default=None, description="male / female / other"),
                sort_by: Optional[str] = Query('created_at', description="Field to sort by: 'created_at', 'email', 'status', 'first_name', 'last_name'"),
                sort_order: Optional[str] = Query('desc', description="Sort order: 'asc' or 'desc'"),
                search: str = None,
                selected_language: Optional[str]=Cookie(default='en'),
                limit: int = Query(default=10, ge=1, le=100, description="Number of suppliers to return"),
                page: int = Query(default=0, ge=0, description="Page number starting from 0")
                  ):
    UserAlias = aliased(models.User)
    SupplierAlias = aliased(models.Supplier)
    LanguageAlias = aliased(models.Language)

    user_list = (
        db.query(UserAlias, SupplierAlias, LanguageAlias)
        .outerjoin(SupplierAlias, SupplierAlias.user_id == UserAlias.id)
        .outerjoin(LanguageAlias, LanguageAlias.id == UserAlias.language_id)
        .filter(UserAlias.role_id == 2)
    )
    
    if search is not None and search != '':
        search_terms = search.split()
        full_name_hash_strings = []
        for i in range(1, len(search_terms)):
            first_name_str = " ".join(search_terms[:i])
            last_name_str = " ".join(search_terms[i:])
            first_name_hash = hash_sort_string(first_name_str)
            last_name_hash = hash_sort_string(last_name_str)
            full_name_hash = f"{first_name_hash} {last_name_hash}"
            full_name_hash_strings.append(full_name_hash)
        filters = [
            or_(
                UserAlias.first_name_hash == hash_sort_string(term),
                UserAlias.last_name_hash == hash_sort_string(term),
                UserAlias.device_type_hash == hash_string(term),
                UserAlias.email.ilike(f"%{term}%"),
                UserAlias.status.ilike(f"%{term}%"),
                SupplierAlias.status.ilike(f"%{term}%"),
                SupplierAlias.stripe_verification_status.ilike(f"%{term}%"),
                func.concat(UserAlias.first_name_hash, literal(" "), UserAlias.last_name_hash).in_(full_name_hash_strings)
            )
            for term in search_terms
        ]
        user_list = user_list.filter(*filters)
    
    if user_status:
        user_list = user_list.filter(UserAlias.status == user_status)
    
    if supplier_status:
        user_list = user_list.filter(SupplierAlias.status == supplier_status)
        
    if supplier_stripe_status:
        user_list = user_list.filter(SupplierAlias.stripe_verification_status == supplier_stripe_status)
    
    if device_type:
        user_list = user_list.filter(UserAlias.device_type_hash == hash_string(device_type))
    
    if gender:
        gender_mapping = {
            "male": "0",
            "female": "1",
            "others": "2"
        }
        user_list = user_list.filter(UserAlias.gender == gender_mapping[gender])
        
    
    if sort_by and sort_order:
        try:
            if sort_by == "first_name":
                sort_by = "first_name_hash"
            elif sort_by == "last_name":
                sort_by = "last_name_hash"
            column = getattr(UserAlias, sort_by, None)
            if not column:
                return BaseController.errorGeneral(messages[selected_language]['something_wrong'], status.HTTP_404_NOT_FOUND)
            order_func = asc if sort_order.lower() == "asc" else desc
            user_list = user_list.order_by(order_func(column))
        except Exception as e:
            return BaseController.errorGeneral(messages[selected_language]['something_wrong'], status.HTTP_404_NOT_FOUND)
    else:
        user_list = user_list.order_by(desc(UserAlias.id))
    
    total_users = user_list.count()
        
    user_list = user_list.offset(page * limit).limit(limit).all()
    
    user_info = []
    if user_list:
        
        for user, supplier, language in user_list:
            user_info.append({
                'id': user.id,
                'email': user.email,
                'first_name': decrypt_data(user.first_name),
                'last_name':decrypt_data(user.last_name),
                'company_name': decrypt_data(user.company_name),
                'gender': get_gender(user.gender),
                'profile_img': user.profile_img,
                'email_verified_at': user.email_verified_at,
                'status': user.status,
                'status_reason': messages[selected_language].get(user.status_reason, user.status_reason),
                'created_at': user.created_at,
                'updated_at': user.updated_at,
                'document_verification_status': user.document_verification_status,
                'language': language.name if language else None,
                'supplier_details': {'id':supplier.id,
                                     'stripe_account_id':supplier.stripe_account_id,
                                     'stripe_bank_account_id':supplier.stripe_bank_account_id,
                                     'stripe_verification_status':supplier.stripe_verification_status,
                                     'created_at':supplier.created_at,
                                     'updated_at':supplier.updated_at,
                                     'status':supplier.status,
                                     'rejection_reason':supplier.rejection_reason,
                                     } if supplier else None
            })

        return PaginationResponse(
            total=total_users,
            page=page,
            per_page=limit,
            data=user_info,
            message=messages[selected_language]['user_listing']
        )
    else:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], 400)

# ──────────────────────────────────────────────
# Email Templates
# ──────────────────────────────────────────────
@router.get("/email-templates", response_model=EmailTemplateInDB, dependencies=[Depends(admin_required)])
def email_template_list(db: Session = Depends(get_db), selected_language: Optional[str]=Cookie(default='en')):
    email_templates = db.query(EmailTemplate).order_by(desc(EmailTemplate.id)).all()
    
    if not email_templates:
        return BaseController.errorGeneral(messages[selected_language]['template_not_found'], 404)
    
    email_template_ls = []
    language_id = (
            db.query(models.Language.id)
            .filter(models.Language.code == selected_language)
            .scalar() or 1
        )
    for email_temp in email_templates:
        default_lang_subject = db.query(EmailTemplateTranslation).filter(EmailTemplateTranslation.template_id == email_temp.id, EmailTemplateTranslation.language_id == language_id).first()
        email_template_ls.append({
            'id': email_temp.id,
            'slug': email_temp.slug,
            'subject': default_lang_subject.subject if default_lang_subject else None,
            'status': email_temp.status,
            'created_at': jsonable_encoder(email_temp.created_at)
        })
    return BaseController.success(email_template_ls, messages[selected_language]['email_template_detail'])   

@router.post("/email-templates/", response_model=EmailTemplateInDB, dependencies=[Depends(admin_required)])
def create_email_template(template: EmailTemplateCreate, db: Session = Depends(get_db), selected_language: Optional[str]=Cookie(default='en')):
    db_template = EmailTemplate(slug=template.slug, status=template.status)
    db.add(db_template)
    db.commit()
    db.refresh(db_template)

    for translation in template.translations:
        db_translation = EmailTemplateTranslation(
            template_id=db_template.id,
            language_id=translation.language_id,
            subject=translation.subject,
            body=translation.body
        )
        db.add(db_translation)

    db.commit()
    db.refresh(db_template)
    return BaseController.success([], messages[selected_language]['email_template_added'])   

@router.get("/email-templates/{template_id}", response_model=EmailTemplateInDB, dependencies=[Depends(admin_required)])
def read_email_template(template_id: int, db: Session = Depends(get_db), selected_language: Optional[str]=Cookie(default='en')):
    email_template = db.query(EmailTemplate).filter(EmailTemplate.id == template_id).first()
    
    if not email_template:
        return BaseController.errorGeneral(messages[selected_language]['template_not_found'], 404)
    
    email_temp_trans = db.query(EmailTemplateTranslation).filter(EmailTemplateTranslation.template_id == template_id).all()
    
    translations = [EmailTemplateTranslationInDB(
        id=email_trans.id,
        subject=email_trans.subject,
        body=email_trans.body,
        language_id= email_trans.language_id
    ) for email_trans in email_temp_trans]
        
    email_template_ls = (EmailTemplateInDB(
            id= email_template.id,
            slug= email_template.slug,
            status= email_template.status,
            created_at= email_template.created_at,
            translations= translations
    ))
    
    return BaseController.success(jsonable_encoder(email_template_ls), messages[selected_language]['email_template_detail'])

@router.put("/email-templates/{template_id}", response_model=EmailTemplateInDB, dependencies=[Depends(admin_required)])
def update_email_template(template_id: int, template: EmailTemplateUpdate, db: Session = Depends(get_db), selected_language: Optional[str]=Cookie(default='en')):
    db_template = db.query(EmailTemplate).filter(EmailTemplate.id == template_id).first()
    if not db_template:
        return BaseController.errorGeneral(messages[selected_language]['template_not_found'], 404)

    db_template.status = template.status
    db.commit()
    
    db.query(EmailTemplateTranslation).filter(EmailTemplateTranslation.template_id == template_id).delete()

    for translation in template.translations:
        db_translation = EmailTemplateTranslation(
            template_id=db_template.id,
            language_id=translation.language_id,
            subject=translation.subject,
            body=translation.body
        )
        db.add(db_translation)

    db.commit()
    db.refresh(db_template)
    return BaseController.success([], messages[selected_language]['email_template_updated']) 

@router.delete("/email-templates/{template_id}", dependencies=[Depends(admin_required)])
def delete_email_template(template_id: int, db: Session = Depends(get_db), selected_language: Optional[str]=Cookie(default='en')):
    db_template = db.query(EmailTemplate).filter(EmailTemplate.id == template_id).first()
    if not db_template:
        return BaseController.errorGeneral(messages[selected_language]['template_not_found'], 404)
    
    db.query(EmailTemplateTranslation).filter(EmailTemplateTranslation.template_id == template_id).delete()
    db.delete(db_template)
    db.commit()
    
    return BaseController.success([], messages[selected_language]['email_template_deleted']) 


# ──────────────────────────────────────────────
# Bookings
# ──────────────────────────────────────────────
@router.get("/get-bookings", dependencies=[Depends(admin_required)])
def get_bookings(
    filters: booking_model.BookingsAdminFilter = Depends(),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
    page: int = Query(default=0, ge=0, description="Page number for pagination"),
    limit: int = Query(default=10, ge=1, le=100, description="Number of bookings to return per page")
):
    bookings_query = db.query(models.Booking)
    if filters.order_status is not None and filters.order_status != '' and filters.order_status.lower() != "all":
        bookings_query = bookings_query.filter(
            models.Booking.status == filters.order_status.lower()
        )
    if filters.order_number is not None and filters.order_number != '':
        bookings_query = bookings_query.filter(
            models.Booking.order_number == filters.order_number
        )
    
    # Date range filters.
    # Smart defaults:
    #   only from_date set -> treat today as end date
    #   only to_date set   -> no start restriction (all bookings up to that date)
    if filters.order_from_date or filters.order_to_date:
        order_filters = []
        if filters.order_from_date:
            order_filters.append(cast(models.Booking.order_date_time, Date) >= convert_date(filters.order_from_date))
        if filters.order_to_date:
            order_filters.append(cast(models.Booking.order_date_time, Date) <= convert_date(filters.order_to_date))
        elif filters.order_from_date:
            order_filters.append(cast(models.Booking.order_date_time, Date) <= datetime.now().date())
        bookings_query = bookings_query.filter(and_(*order_filters))

    if filters.booking_from_date or filters.booking_to_date:
        booking_filters = []
        if filters.booking_from_date:
            booking_filters.append(cast(models.Booking.booking_date_time, Date) >= convert_date(filters.booking_from_date))
        if filters.booking_to_date:
            booking_filters.append(cast(models.Booking.booking_date_time, Date) <= convert_date(filters.booking_to_date))
        elif filters.booking_from_date:
            booking_filters.append(cast(models.Booking.booking_date_time, Date) <= datetime.now().date())
        bookings_query = bookings_query.filter(and_(*booking_filters))
    
    if filters.search is not None and filters.search != '':
        search_term = f"%{filters.search}%"
        bookings_query = bookings_query.filter(
            or_(
                models.Booking.customer_name.ilike(search_term),
                models.Booking.supplier_name.ilike(search_term),
                models.Booking.order_number.ilike(search_term),
                models.Booking.status.ilike(search_term.lower())
            )
        )

    if filters.expense_classification and str(filters.expense_classification).strip().lower() != "all":
        bookings_query = bookings_query.filter(
            models.Booking.expense_classification == str(filters.expense_classification).strip(),
            models.Booking.user_id.in_(db.query(models.Supplier.user_id)),
        )

    total_bookings = bookings_query.count()
    sort_columns = {
        "order_number": models.Booking.order_number,
        "status": models.Booking.status,
        "order_date_time": models.Booking.order_date_time,
        "booking_date_time": models.Booking.booking_date_time,
        "customer_name": models.Booking.customer_name,
        "supplier_name": models.Booking.supplier_name,
        "total_price": models.Booking.total_price
        
    }
    if filters.sort_by in sort_columns:
        sort_column = sort_columns[filters.sort_by]

        if filters.sort_order == 'asc':
            bookings_query = bookings_query.order_by(asc(sort_column))
        else:
            bookings_query = bookings_query.order_by(desc(sort_column))
    else:
        bookings_query = bookings_query.order_by(desc(models.Booking.created_at))
    bookings = bookings_query.offset(page * limit).limit(limit).all()

    buyer_ids = {b.user_id for b in bookings if b.user_id}
    supplier_buyer_ids = set()
    if buyer_ids:
        supplier_buyer_ids = {
            row[0]
            for row in db.query(models.Supplier.user_id)
            .filter(models.Supplier.user_id.in_(buyer_ids))
            .all()
        }

    response_data = []
    for booking in bookings:
        is_customer_supplier = bool(booking.user_id and booking.user_id in supplier_buyer_ids)
        response_data.append(admin.GetAdminBookings(
            id = booking.id,
            order_number=booking.order_number,
            booking_date_time=booking.booking_date_time,
            customer_name=booking.customer_name,
            customer_company_name=decrypt_data(booking.user1.company_name) if booking.user1 else None,
            total_price=compute_booking_total(booking),
            order_date_time=booking.order_date_time,
            supplier_name=booking.supplier_name,
            supplier_company_name=decrypt_data(booking.supplier.user.company_name) if booking.supplier and booking.supplier.user else None,
            status=booking.status,
            currency_code = booking.currency_code,
            currency_symbol = booking.currency_symbol,
            profile_img = booking.user1.profile_img if booking.user1 else None,
            supplier_profile_img = booking.supplier.user.profile_img if booking.supplier else None,
            expense_classification = booking.expense_classification or "2",
            is_customer_supplier = is_customer_supplier,
        ))

    return PaginationResponse(
        total=total_bookings,
        page=page,
        per_page=limit,
        data=response_data,
        message=messages[selected_language]['user_bookings']
    )

@router.get("/bookings/export", dependencies=[Depends(admin_required)])
async def export_bookings(selected_language: Optional[str] = Cookie(default='en'), db: Session = Depends(get_db)):
    try:
        # Fetch records from the Booking model
        bookings = db.query(models.Booking).all()

        if not bookings:
            return BaseController.errorGeneral(messages[selected_language]['booking_not_found'], status.HTTP_404_NOT_FOUND)

        # Prepare the data for CSV
        booking_data = []
        for booking in bookings:
            booking_data.append({
                "id": booking.id,
                "user_id": booking.user_id,
                "supplier_id": booking.supplier_id,
                "status": booking.status,
                "customer_name": booking.customer_name,
                "booking_address": booking.booking_address,
                "supplier_name": booking.supplier_name,
                "total_price": booking.total_price,
                "order_date_time": booking.order_date_time,
                "created_at": booking.created_at
            })

        df = pd.DataFrame(booking_data)

        buffer = io.StringIO()
        df.to_csv(buffer, index=False)
        buffer.seek(0)

        headers = {
            'Content-Disposition': 'attachment; filename=bookings_export.csv'
        }
        
        return StreamingResponse(buffer, media_type='text/csv', headers=headers)
    except Exception as e:
        return BaseController.errorGeneral(messages[selected_language]['something_wrong'], status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    
# ──────────────────────────────────────────────
# Categories
# ──────────────────────────────────────────────
@router.post("/add_category", dependencies=[Depends(admin_required)])
async def create_category(
    slug: str,
    parent_id: Optional[int] = None,
    icon: Union[UploadFile, str, None] = File(None),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    cat_exist = db.query(models.Category).filter(models.Category.slug == slug).first()
    if cat_exist:
        return BaseController.errorGeneral(messages[selected_language]['category_exist'], 400)
    
    category_icon = None
    if icon and not isinstance(icon, str):
        if not icon.content_type.startswith("image/"):
            return BaseController.errorGeneral(
                messages[selected_language]['invalid_file_format'], 400)
        
        if not allowed_file(icon.filename):
            return BaseController.errorGeneral(messages[selected_language]['invalid_extension'], 400)
        
        if not os.path.exists(os.path.join(settings.FILE_DIR_PATH, CAT_ICONS_DIR)):
            os.makedirs(os.path.join(settings.FILE_DIR_PATH, CAT_ICONS_DIR))

        file_extension = icon.filename.split(".")[-1]
        file_name = f"{str(uuid.uuid4())}.{file_extension}"
        file_path = os.path.join(CAT_ICONS_DIR, file_name)

        with open(os.path.join(settings.FILE_DIR_PATH, file_path), "wb") as f:
            f.write(await icon.read())
        
        category_icon = file_path
    
    cat_info = models.Category(slug=slug, parent_id = parent_id, category_icon = category_icon)
    db.add(cat_info)
    db.commit()
    db.refresh(cat_info)
    
    return BaseController.success(jsonable_encoder(cat_info),messages[selected_lang]['category_created'])

@router.put("/update_category/{category_id}", dependencies=[Depends(admin_required)])
async def update_category(
    category_id: int,
    slug: Optional[str] = Form(None),
    parent_id: Optional[int] = Form(None),
    status: Optional[str] = Form(None),
    cat_icon:  Union[UploadFile, str, None] = File(None),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    category = db.query(models.Category).filter(models.Category.id == category_id).first()
    if not category:
        return BaseController.errorGeneral(messages[selected_language]['category_not_found'], status.HTTP_404_NOT_FOUND)

    if slug is not None:
        category.slug = slug

    if parent_id is not None:
        if parent_id == 0:
            category.parent_id = None
        else:
            category.parent_id = parent_id
    
    if status is not None:
        category.status = status    
    
    if cat_icon and not isinstance(cat_icon, str):
        old_icon = category.category_icon
        if not cat_icon.content_type.startswith("image/"):
            return BaseController.errorGeneral(
                messages[selected_language]['invalid_file_format'], 400)
        
        if not allowed_file(cat_icon.filename):
            return BaseController.errorGeneral(messages[selected_language]['invalid_extension'], 400)
        
        if not os.path.exists(os.path.join(settings.FILE_DIR_PATH, CAT_ICONS_DIR)):
            os.makedirs(os.path.join(settings.FILE_DIR_PATH, CAT_ICONS_DIR))

        file_extension = cat_icon.filename.split(".")[-1]
        file_name = f"{str(uuid.uuid4())}.{file_extension}"
        file_path = os.path.join(CAT_ICONS_DIR, file_name)

        with open(os.path.join(settings.FILE_DIR_PATH, file_path), "wb") as f:
            f.write(await cat_icon.read())
            
        category.category_icon = file_path 
        
        # Check if the old icon exists, and delete it
        if old_icon:
            old_icon_path = os.path.join(settings.FILE_DIR_PATH, old_icon)
            if os.path.exists(old_icon_path):
                os.remove(old_icon_path)   
        
    db.commit()

    return BaseController.success([], messages[selected_language]['category_updated'])


@router.post("/category/{category_id}/translation", response_model=admin.CategoryTranslationCreate, dependencies=[Depends(admin_required)])
async def add_category_translation(
    category_id: int,
    name: str,
    language_id: int,
    icon_file: UploadFile = File(None),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    category_exists = db.query(models.Category).filter_by(id=category_id).first()
    language_exists = db.query(models.Language).filter_by(id=language_id).first()

    if not category_exists:
        return BaseController.errorGeneral(messages[selected_language]['category_not_found'], 404)
    if not language_exists:
        return BaseController.errorGeneral(messages[selected_language]['language_not_found'], 404)
    translation = (
        db.query(models.CategoryTranslation)
        .filter_by(category_id=category_id, language_id=language_id)
        .first()
    )
    if translation:
        if name:
            translation.name = name
        if icon_file and not isinstance(icon_file, str):
            if not allowed_file(icon_file.filename):
                return BaseController.errorGeneral(messages[selected_language]['invalid_extension'], 400)
            
            if translation.icon_path:
                old_icon_path = os.path.join(settings.FILE_DIR_PATH, translation.icon_path)
                if os.path.exists(old_icon_path):
                    os.remove(old_icon_path)

            # Save the new icon file
            file_extension = icon_file.filename.split(".")[-1]
            file_name = f"category_{category_id}_lang_{language_id}.{file_extension}"
            file_path = os.path.join(CAT_ICONS_DIR, file_name)

            with open(os.path.join(settings.FILE_DIR_PATH, file_path), "wb") as f:
                f.write(await icon_file.read())
            
            translation.icon_path = str(file_path)

        translation.updated_at = datetime.now()

        db.commit()
    else:
        icon_path = None
        if icon_file:
            if not allowed_file(icon_file.filename):
                return BaseController.errorGeneral(messages[selected_language]['invalid_extension'], 400)
            
            file_extension = icon_file.filename.split(".")[-1]
            file_name = f"category_{category_id}_lang_{language_id}.{file_extension}"
            file_path = os.path.join(CAT_ICONS_DIR, file_name)

            with open(os.path.join(settings.FILE_DIR_PATH, file_path), "wb") as f:
                f.write(await icon_file.read())
            icon_path = str(file_path)

        new_translation = models.CategoryTranslation(
            category_id=category_id,
            language_id=language_id,
            name=name,
            icon_path=icon_path,
            created_at=datetime.now(),
            updated_at=datetime.now()
        )

        db.add(new_translation)
        db.commit()

    return BaseController.success([],messages[selected_lang]['category_created'])

@router.delete("/category/{category_id}/translation", dependencies=[Depends(admin_required)])
def delete_category_translation(
    category_id: int,
    language_id: int,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
):
    """Remove a single per-language translation from a category (and its icon file)."""
    category_exists = db.query(models.Category).filter_by(id=category_id).first()
    if not category_exists:
        return BaseController.errorGeneral(messages[selected_language]['category_not_found'], 404)

    translation = (
        db.query(models.CategoryTranslation)
        .filter_by(category_id=category_id, language_id=language_id)
        .first()
    )
    if not translation:
        return BaseController.errorGeneral(messages[selected_language]['language_not_found'], 404)

    if translation.icon_path:
        icon_full_path = os.path.join(settings.FILE_DIR_PATH, translation.icon_path)
        if os.path.exists(icon_full_path):
            os.remove(icon_full_path)

    db.delete(translation)
    db.commit()

    return BaseController.success([], messages[selected_language]['category_updated'])


@router.put("/category/{category_id}/upd_trans", dependencies=[Depends(admin_required)])
async def update_category_translation(
    category_id: int,
    language_id: int,
    name: Optional[str] = Form(None),
    icon_file: Union[UploadFile, str, None] = File(None),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    category_exists = db.query(models.Category).filter_by(id=category_id).first()
    language_exists = db.query(models.Language).filter_by(id=language_id).first()
    if not category_exists:
        return BaseController.errorGeneral(messages[selected_language]['category_not_found'], 404)
    if not language_exists:
        return BaseController.errorGeneral(messages[selected_language]['language_not_found'], 404)

    translation = (
        db.query(models.CategoryTranslation)
        .filter_by(category_id=category_id, language_id=language_id)
        .first()
    )
    
    if translation:
        if name:
            translation.name = name
        if icon_file and not isinstance(icon_file, str):
            if not allowed_file(icon_file.filename):
                return BaseController.errorGeneral(messages[selected_language]['invalid_extension'], 400)
            
            if translation.icon_path:
                old_icon_path = os.path.join(settings.FILE_DIR_PATH, translation.icon_path)
                if os.path.exists(old_icon_path):
                    os.remove(old_icon_path)

            # Save the new icon file
            file_extension = icon_file.filename.split(".")[-1]
            file_name = f"category_{category_id}_lang_{language_id}.{file_extension}"
            file_path = os.path.join(CAT_ICONS_DIR, file_name)

            with open(os.path.join(settings.FILE_DIR_PATH, file_path), "wb") as f:
                f.write(await icon_file.read())
            
            translation.icon_path = str(file_path)

        translation.updated_at = datetime.now()
    else:
        if not name:
            return BaseController.errorGeneral(messages[selected_language]['invalid_category_data'], status.HTTP_400_BAD_REQUEST)
        new_translation = models.CategoryTranslation(
            category_id=category_id,
            language_id=language_id,
            name=name,
            created_at=datetime.now(),
            updated_at=datetime.now()
        )
        if icon_file and not isinstance(icon_file, str):
            if not allowed_file(icon_file.filename):
                return BaseController.errorGeneral(messages[selected_language]['invalid_extension'], 400)

            # Save the new icon file
            file_extension = icon_file.filename.split(".")[-1]
            file_name = f"category_{category_id}_lang_{language_id}.{file_extension}"
            file_path = os.path.join(CAT_ICONS_DIR, file_name)

            with open(os.path.join(settings.FILE_DIR_PATH, file_path), "wb") as f:
                f.write(await icon_file.read())
            
            new_translation.icon_path = str(file_path)
        db.add(new_translation)
    db.commit()

    return BaseController.success([], messages[selected_language]['category_updated'])


def map_category_to_dict(category: models.Category, db: Session, language_id) -> dict:
    default_lang_subject = db.query(models.CategoryTranslation).filter(models.CategoryTranslation.category_id == category.id, models.CategoryTranslation.language_id == language_id).first()
    
    """Convert a Category model to a dictionary, including nested sub-categories, with datetime as strings."""
    return {
        "id": category.id,
        "parent_id": category.parent_id,
        "slug": category.slug,
        'name': default_lang_subject.name if default_lang_subject else None,       
        "status": category.status,
        "category_icon": category.category_icon,
        "created_at": category.created_at.isoformat() if category.created_at else None,
        "updated_at": category.updated_at.isoformat() if category.updated_at else None,
        "sub_categories": [
            map_category_to_dict(sub_category, db, language_id)
            for sub_category in db.query(models.Category).filter(models.Category.parent_id == category.id).all()
        ]
    }


@router.get("/categories", dependencies=[Depends(admin_required)])
def get_categories(
    search_key: Optional[str] = Query(None, description="Search for categories by keyword"),
    cat_id: Optional[int] = Query(None, description="Id of category to exclude from response"),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
    ):
    query = (
        db.query(models.Category)
        .filter(models.Category.parent_id == None)
    )
    if cat_id:
        query = query.filter(
            models.Category.id != cat_id
        )
    
    if search_key:
        search_term = f"%{search_key}%"
        query = query.filter(
            or_(
                models.Category.slug.ilike(search_term)
            )
        )

    # Step 4: Fetch the results
    category_list = query.order_by(desc(models.Category.id)).all()
    
    language_id = (
            db.query(models.Language.id)
            .filter(models.Language.code == selected_language)
            .scalar() or 1
        )
    
    if category_list:
        categories_response = [map_category_to_dict(category, db, language_id) for category in category_list]
        return BaseController.success(categories_response, messages[selected_language]['category_listing'])
    else:
        return BaseController.success([], messages[selected_language]['category_listing'])

@router.get("/category/{category_id}/translation", dependencies=[Depends(admin_required)])
def get_category(category_id: int, db: Session = Depends(get_db) ,selected_language: Optional[str] = Cookie(default='en')):
    category_data = db.query(models.Category).filter(models.Category.id == category_id).first()
    if not category_data:
        return BaseController.errorGeneral(messages[selected_language]['category_not_found'], status.HTTP_401_UNAUTHORIZED)
    has_sub_categories = db.query(models.Category).filter(models.Category.parent_id == category_id).first() is not None
    category_response = {
        "slug": category_data.slug,
        "category_icon": category_data.category_icon,
        "created_at": category_data.created_at,
        "parent_id": category_data.parent_id,
        "id": category_data.id,
        "status": category_data.status,
        "updated_at": category_data.updated_at,
        "has_sub_categories": has_sub_categories
    }
    
    category_trans_list = db.query(models.CategoryTranslation).filter(models.CategoryTranslation.category_id == category_id).all()
    
    if category_trans_list:
        category_info = [
            admin.CategoryTranslation(
                id=category_trans.id,
                category_id=category_trans.category_id,
                language_id=category_trans.language_id,
                language_name=category_trans.language.name,
                name=category_trans.name,
                icon_path=category_trans.icon_path if category_trans.icon_path else None,
                created_at=category_trans.created_at,
                updated_at=category_trans.updated_at if category_trans.updated_at else None,
            )
            for category_trans in category_trans_list
        ]
    else:
        category_info = []
    combined_response = {
        "category": jsonable_encoder(category_response),
        "translations": jsonable_encoder(category_info)
    }
        
    return BaseController.success(combined_response, messages[selected_language]['category_listing'])
    
def map_category(category: models.Category, selected_language, db: Session) -> admin.GetCategories:
    translation = (
        db.query(models.CategoryTranslation)
        .filter(models.CategoryTranslation.category_id == category.id)
        .first()
    )
    
    if not translation:
        return BaseController.errorGeneral(messages[selected_language]['translation_not_found'], status.HTTP_401_UNAUTHORIZED)

    category.sub_categories = (
        db.query(models.Category).join(models.CategoryTranslation)
        .filter(models.Category.parent_id == category.id)
        .all()
    )
    return admin.GetCategories(
        id=category.id,
        parent_id=category.parent_id,
        name=translation.name,
        status=category.status,
        icon_path=translation.icon_path,
        sub_categories=[map_category(sub_category, selected_language, db) for sub_category in category.sub_categories] if category.sub_categories else []
    )

# ──────────────────────────────────────────────
# Suppliers
# ──────────────────────────────────────────────
@router.put("/update_supplier/{supplier_id}", dependencies=[Depends(admin_required)])
def update_user(supplier_id: int,
                form_data: suppliers.updateStatus,
                background_tasks: BackgroundTasks,
                db: Session = Depends(get_db),
                selected_language: Optional[str]=Cookie(default='en')):
    try:
        exist_user = (
            db.query(models.Supplier)
            .join(models.User, models.Supplier.user_id == models.User.id)
            .join(models.Language, models.User.language_id == models.Language.id)
            .filter(models.Supplier.id == supplier_id)
            .first()
        )
        if not exist_user:
            return BaseController.errorGeneral(messages[selected_language]['user_not_found'], 400)
        
        exist_user.status = form_data.status
        exist_user.rejection_reason = form_data.status_reason,
        db.commit()
        if exist_user.status == "Approved":
            supplier_template_content = get_email_template_content(db, exist_user.user.language_id, 'supplier-status-approved')
        else:
            supplier_template_content = get_email_template_content(db, exist_user.user.language_id, 'supplier-status')
        if supplier_template_content:
            background_tasks.add_task(
                send_supplier_status_email,
                email=exist_user.user.email,
                subject=supplier_template_content.subject,
                body=supplier_template_content.body,
                first_name=decrypt_data(exist_user.user.first_name),
                last_name=decrypt_data(exist_user.user.last_name),
                status = messages.get(exist_user.user.language.code, {}).get(exist_user.status.lower().replace(" ", "_"), exist_user.status),
                selected_language=exist_user.user.language.code,
            )

        return BaseController.success([],messages[selected_lang]['supplier_detail_updated'])
    except JWTError:
        return BaseController.errorGeneral(messages[selected_language]['something_went_wrong'])
    
# ──────────────────────────────────────────────
# User Info Helpers
# ──────────────────────────────────────────────
def _derive_admin_order_type(booking_items) -> str:
    """Derive an order-level type from the item_type"""
    types = {getattr(bi, 'item_type', '0') for bi in booking_items}
    if not types:
        return '0'
    if len(types) == 1:
        return next(iter(types))
    return '2'


def _build_supplier_schedules(db: Session, supplier):
    week_days = db.query(models.WeekDay).all()
    schedules = (
        db.query(models.SupplierItemSchedule)
        .join(models.Supplier)
        .filter(models.Supplier.id == supplier.id)
        .all()
    )
    schedules_by_day = {day.id: [] for day in week_days}
    for schedule in schedules:
        schedules_by_day[schedule.day_id].append(schedule)
    return [
        supplier_model.SupplierScheduleModel(
            day_id=day.id,
            day_name=day.slug,
            day_on=bool(schedules_by_day[day.id]) and not all(schedule.availability == '0' for schedule in schedules_by_day[day.id]),
            schedules=[
                supplier_model.ScheduleModel(
                    id=schedule.id,
                    supplier_id=schedule.supplier_id,
                    day_id=schedule.day_id,
                    availability=int(schedule.availability),
                    start_time=schedule.start_time.strftime("%H:%M"),
                    end_time=schedule.end_time.strftime("%H:%M")
                )
                for schedule in schedules_by_day[day.id]
            ]
        )
        for day in week_days
    ]


def _build_reviews(db: Session, user_id, reviewer_type):
    total_reviews, avg_reviews = (
        db.query(
            func.count(models.t_booking_reviews.c.receiver_id),
            coalesce(func.avg(models.t_booking_reviews.c.rating_value), 0)
        )
        .filter(models.t_booking_reviews.c.receiver_id == user_id, models.t_booking_reviews.c.reviewer_type == reviewer_type)
        .first()
    )
    join_col = models.t_booking_reviews.c.sender_id if reviewer_type == "1" else models.t_booking_reviews.c.receiver_id
    reviews = (
        db.query(
            models.t_booking_reviews.c.id,
            models.t_booking_reviews.c.booking_id,
            models.t_booking_reviews.c.review_text,
            models.t_booking_reviews.c.rating_value,
            models.t_booking_reviews.c.created_at,
            models.User.first_name.label('first_name'),
            models.User.last_name.label('last_name'),
            models.User.company_name.label('company_name'),
            models.User.profile_img.label('profile_img'),
        )
        .join(models.User, models.User.id == join_col)
        .filter(models.t_booking_reviews.c.receiver_id == user_id, models.t_booking_reviews.c.reviewer_type == reviewer_type)
        .order_by(desc(models.t_booking_reviews.c.created_at))
        .limit(10)
        .all()
    )
    review_list = [
        supplier_model.ReviewModel(
            id=review.id,
            booking_id=review.booking_id,
            review_text=review.review_text,
            rating_value=review.rating_value,
            first_name=decrypt_data(review.first_name),
            last_name=decrypt_data(review.last_name),
            company_name=decrypt_data(review.company_name),
            profile_img=review.profile_img,
            created_at=review.created_at
        )
        for review in reviews
    ]
    return total_reviews, avg_reviews, review_list


def _group_subscriptions_by_plan(subscription_data: dict) -> dict:
    """
    Bucket the flat picker lists from get_added_subscriptions() into per-plan groups by
    looking for 'gold' / 'silver' / 'diamond' as a substring in productId. Anything else
    falls under standard. Result shape mirrors SUBSCRIPTION_PLAN_DEFAULTS: each plan holds
    the four slot lists (annual / monthly / ios_annual / ios_monthly).
    """
    grouped = {
        plan: {slot: [] for slot in SUBSCRIPTION_SLOTS}
        for plan in SUBSCRIPTION_PLAN_TYPES
    }
    for slot in SUBSCRIPTION_SLOTS:
        for product in subscription_data.get(slot) or []:
            product_id = (product.get("productId") or "").lower()
            if "gold" in product_id:
                plan = "gold"
            elif "silver" in product_id:
                plan = "silver"
            elif "diamond" in product_id:
                plan = "diamond"
            else:
                plan = "standard"
            grouped[plan][slot].append(product)
    return grouped


def _resolve_user_subscription_products(user) -> dict:
    """
    Return a fully-shaped {plan_type: {slot: product_id}} dict.

    Resolution order per slot:
      1. user.subscription_products[plan][slot] (admin-saved override)
      2. legacy column (only for standard plan, preserves existing referral discount overrides)
      3. SUBSCRIPTION_PLAN_DEFAULTS[plan][slot] (tier-specific default, e.g. gold_annually)
    """
    legacy_standard = {
        "annual": user.annual_subscription_product_id,
        "monthly": user.monthly_subscription_product_id,
        "ios_annual": user.ios_annual_subscription_product_id,
        "ios_monthly": user.ios_monthly_subscription_product_id,
    }
    stored = user.subscription_products or {}
    resolved = {}
    for plan in SUBSCRIPTION_PLAN_TYPES:
        plan_stored = stored.get(plan) or {}
        plan_resolved = {}
        for slot in SUBSCRIPTION_SLOTS:
            value = plan_stored.get(slot)
            if not value and plan == "standard":
                value = legacy_standard[slot]
            if not value:
                value = SUBSCRIPTION_PLAN_DEFAULTS[plan][slot]
            plan_resolved[slot] = value
        resolved[plan] = plan_resolved
    return resolved


def _build_supplier_subscriptions(db: Session, supplier):
    subscriptions = db.query(models.SupplierSubscription).join(models.Supplier).filter(models.Supplier.id == supplier.id).all()
    active_subscription = (
        db.query(models.SupplierSubscription)
        .join(models.Supplier)
        .filter(models.Supplier.id == supplier.id)
        .filter(models.SupplierSubscription.status == "active")
        .order_by(desc(models.SupplierSubscription.created_at))
        .first()
    )
    current_subscription_type = None
    if active_subscription:
        current_subscription_type = "Personal Subscription" if active_subscription.purchase_response else "Organization Access"
    subscription_list = [
        admin.SupplierSubscription(
            id=subscription.id,
            order_id=subscription.order_id,
            supplier_id=subscription.supplier_id,
            product_id=subscription.product_id,
            purchase_time=subscription.purchase_time,
            purchase_state=subscription.purchase_state,
            purchase_token=subscription.purchase_token,
            quantity=subscription.quantity,
            auto_renewing=subscription.auto_renewing,
            acknowledged=subscription.acknowledged,
            purchase_response=subscription.purchase_response,
            status=subscription.status,
            personal_subscription=subscription.personal_subscription,
            subscription_type=subscription.subscription_type,
            created_at=subscription.created_at,
            updated_at=subscription.updated_at
        ) for subscription in subscriptions
    ]
    return subscription_list, current_subscription_type


def _build_supplier_items(db: Session, supplier, selected_language):
    items_list = (
        db.query(models.SupplierItem)
        .join(models.Supplier)
        .filter(models.Supplier.id == supplier.id)
        .filter(models.SupplierItem.status == '1')
        .filter(models.SupplierItem.deleted_at.is_(None))
        .options(joinedload(models.SupplierItem.sub_categories))
        .order_by(desc(models.SupplierItem.created_at))
        .limit(10)
        .all()
    )
    language_id = db.query(models.Language.id).filter(models.Language.code == selected_language).scalar() or 1
    category_ids = {category.id for item in items_list for category in item.sub_categories}
    category_ids.update({category.parent_id for item in items_list for category in item.sub_categories if category.parent_id is not None})
    translations = (
        db.query(models.CategoryTranslation)
        .filter(models.CategoryTranslation.category_id.in_(category_ids), models.CategoryTranslation.language_id == language_id)
        .all()
    )
    translation_map = {t.category_id: t.name for t in translations}
    item_unit_mapping = {"0": "Hourly", "1": "Quantity"}
    return [
        supplier_model.SupplierItemModel(
            id=item.id,
            currency_id=supplier.user.currency_id,
            currency_code=supplier.user.currency.code if supplier.user.currency else None,
            currency_symbol=supplier.user.currency.symbol if supplier.user.currency else None,
            supplier_id=item.supplier_id,
            item_name=item.item_name,
            item_unit=item_unit_mapping[item.item_unit],
            item_type=item.item_type,
            item_quantity=item.item_quantity,
            item_price=f"{round(float(item.item_price), 2):.2f}" if item.item_price is not None else None,
            status=item.status,
            item_image=item.item_image,
            item_images=[
                supplier_model.SupplierItemImageModel(
                    id=img.id,
                    image_url=img.image_url,
                    sort_order=img.sort_order,
                    is_flagged=img.is_flagged,
                    flagged_categories=img.flagged_categories,
                    flagged_reason=img.flagged_reason,
                )
                for img in sorted(
                    [i for i in (item.images or []) if i.deleted_at is None],
                    key=lambda i: (i.sort_order, i.id),
                )
            ],
            categories=[
                supplier_model.CategoryModel(
                    category_id=category.parent_id,
                    category_name=translation_map.get(category.parent_id, category.parent.slug) if category.parent_id else category.parent.slug,
                    category_icon=category.parent.category_icon,
                    sub_category_id=category.id,
                    sub_category_name=translation_map.get(category.id, category.slug)
                )
                for category in item.sub_categories
            ]
        )
        for item in items_list
    ]


def _build_bank_account_info(supplier):
    bank_account_info = {"created": None, "country": None, "default_currency": None, "details_submitted": False}
    if supplier and supplier.stripe_account_id:
        supplier_stripe_info = stripe.Account.retrieve(
            supplier.stripe_account_id,
            api_key=stripe_api_key_for(supplier),
        )
        bank_account_info["created"] = supplier_stripe_info.get('created', None)
        bank_account_info["country"] = supplier_stripe_info.get('country', None)
        bank_account_info["default_currency"] = supplier_stripe_info.get('default_currency', None)
        bank_account_info["details_submitted"] = supplier_stripe_info.get('details_submitted', False)
    return bank_account_info


@router.get("/user-info/{user_id}", dependencies=[Depends(admin_required)])
def get_user_info(
    user_id: int,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)

    supplier = db.query(models.Supplier).filter(models.Supplier.user_id == user.id).first()
    current_subscription_type = None

    if supplier:
        total_items_count, min_price, max_price = (
            db.query(
                func.count(models.SupplierItem.id),
                func.coalesce(func.min(models.SupplierItem.item_price), 0),
                func.coalesce(func.max(models.SupplierItem.item_price), 0)
            )
            .join(models.Supplier)
            .filter(
                models.Supplier.id == supplier.id,
                models.SupplierItem.deleted_at.is_(None),
            )
            .first()
        )
        supplier_schedules = _build_supplier_schedules(db, supplier)
        total_supplier_reviews, total_average_reviews, review_as_supplier = _build_reviews(db, supplier.user.id, "1")
        total_customer_reviews, _, _ = _build_reviews(db, supplier.user.id, "0")
        supplier_subscription, current_subscription_type = _build_supplier_subscriptions(db, supplier)
        items_with_categories = _build_supplier_items(db, supplier, selected_language)
    else:
        total_items_count = min_price = max_price = total_supplier_reviews = total_average_reviews = 0
        items_with_categories = supplier_schedules = review_as_supplier = supplier_subscription = []
        total_customer_reviews, total_average_reviews, _ = _build_reviews(db, user.id, "0")

    _, _, review_as_customer = _build_reviews(db, user.id, "0")

    language_id = db.query(models.Language.id).filter(models.Language.code == selected_language).scalar() or 1
    default_lang_document_type = db.query(models.DocumentTypeTranslation).filter(models.DocumentTypeTranslation.document_type_id == user.document_type_id, models.DocumentTypeTranslation.language_id == language_id).first()
    default_lang_org_name = None
    if user.organization_id:
        default_lang_org_name = db.query(models.OrganizationTranslation).filter(models.OrganizationTranslation.organization_id == user.organization_id, models.OrganizationTranslation.language_id == language_id).first()
    bank_account_info = _build_bank_account_info(supplier)
    if user.document_verification_response and isinstance(user.document_verification_response, str):
        document_verification_response = decrypt_data(user.document_verification_response)
    else:
        document_verification_response = None
    subscription_data = get_added_subscriptions()

    supplier_info = supplier_model.SupplierInfoAdminModel(
        id=supplier.user_id if supplier else user.id,
        first_name=decrypt_data(supplier.user.first_name) if supplier else decrypt_data(user.first_name),
        last_name=decrypt_data(supplier.user.last_name) if supplier else decrypt_data(user.last_name),
        information=decrypt_data(supplier.user.information) if supplier else decrypt_data(user.information),
        email_verified_at=user.email_verified_at,
        about=user.about,
        email=user.email,
        user_status=user.status,
        status_reason=messages[selected_language].get(user.status_reason, user.status_reason),
        company_name=decrypt_data(user.company_name),
        gender=user.gender,
        time_zone=decrypt_data(user.time_zone),
        document_type_id=user.document_type_id,
        document_type_name=default_lang_document_type.document_type_name if default_lang_document_type else None,
        document_number=decrypt_data(user.document_number),
        document_img=user.document_img,
        organization_id=user.organization_id if user.organization else None,
        organization_slug=user.organization.slug if user.organization else None,
        organization_name=default_lang_org_name.org_name if default_lang_org_name else None,
        organization_document_number=decrypt_data(user.organization_document_number) if user.organization else None,
        organization_document_img=user.organization_document_img if user.organization else None,
        selected_language=user.language.name,
        selected_language_icon=user.language.icon,
        ip_address=decrypt_data(user.ip_address),
        nationality=user.nationality.country_name if user.nationality else None,
        currency=user.currency.name if user.currency else None,
        created_at=user.created_at if user.created_at else None,
        updated_at=user.updated_at if user.updated_at else None,
        deleted_at=user.deleted_at if user.deleted_at else None,
        address=decrypt_data(supplier.user.address) if supplier else decrypt_data(user.address),
        city=decrypt_data(supplier.user.city) if supplier else decrypt_data(user.city),
        country_id=supplier.user.country_id if supplier else user.country_id,
        country_name=user.country.country_name if user.country else None,
        apartment_zone=decrypt_data(supplier.user.apartment_zone) if supplier else decrypt_data(user.apartment_zone),
        additional_direction=decrypt_data(supplier.user.additional_direction) if supplier else decrypt_data(user.additional_direction),
        is_supplier=True if supplier else False,
        supplier_id=supplier.id if supplier else None,
        supplier_status=supplier.status if supplier else None,
        supplier_rejection_reason=supplier.rejection_reason if supplier else None,
        stripe_account_id=supplier.stripe_account_id if supplier else None,
        stripe_bank_account_id=supplier.stripe_bank_account_id if supplier else None,
        stripe_verification_status=supplier.stripe_verification_status if supplier else None,
        total_items=total_items_count,
        total_review_as_customer=total_customer_reviews,
        total_review_as_supplier=total_supplier_reviews,
        supplier_image=supplier.user.profile_img if supplier else None,
        customer_image=user.profile_img,
        is_customer_profile_completed=True if user.status == "Approved" else False,
        total_average_reviews=total_average_reviews,
        min_price=f"{round(min_price, 2):.2f}" if min_price else None,
        max_price=f"{round(max_price, 2):.2f}" if max_price else None,
        latitude=decrypt_data(supplier.user.latitude) if supplier else decrypt_data(user.latitude),
        longitude=decrypt_data(supplier.user.longitude) if supplier else decrypt_data(user.longitude),
        portfolio_items=items_with_categories,
        supplier_nationality_flag=supplier.user.country.country_icon if supplier and supplier.user.country else None,
        customer_nationality_flag=user.country.country_icon if user.country else None,
        supplier_user_status=supplier.user.status if supplier else None,
        supplier_user_status_reason=messages[selected_language].get(supplier.user.status_reason, supplier.user.status_reason) if supplier else None,
        supplier_schedules=supplier_schedules,
        review_as_supplier=review_as_supplier,
        review_as_customer=review_as_customer,
        supplier_subscription=supplier_subscription,
        bank_account_info=bank_account_info,
        document_verification_response=document_verification_response,
        document_verification_status=user.document_verification_status,
        document_verification_reason=user.document_verification_reason,
        referral_code=user.referral_code,
        referred_by_code=user.referred_by,
        annual_subscription_product_id=user.annual_subscription_product_id,
        monthly_subscription_product_id=user.monthly_subscription_product_id,
        ios_annual_subscription_product_id=user.ios_annual_subscription_product_id,
        ios_monthly_subscription_product_id=user.ios_monthly_subscription_product_id,
        subscription_products=_resolve_user_subscription_products(user),
        annual_subscriptions_list=subscription_data.get("annual"),
        monthly_subscriptions_list=subscription_data.get("monthly"),
        ios_annual_subscriptions_list=subscription_data.get("ios_annual"),
        ios_monthly_subscriptions_list=subscription_data.get("ios_monthly"),
        subscription_plans_picker=_group_subscriptions_by_plan(subscription_data),
        subscription_type=current_subscription_type,
        website_url=decrypt_data(supplier.user.website_url) if supplier else decrypt_data(user.website_url),
        instagram_url=decrypt_data(supplier.user.instagram_url) if supplier else decrypt_data(user.instagram_url),
    )

    return BaseController.success(jsonable_encoder(supplier_info), messages[selected_language]['supplier_info'])

@router.post("/organization/", dependencies=[Depends(admin_required)])
async def add_organization(
    org_data: admin.OrganizationCreate,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    organization = db.query(models.Organization).filter_by(slug=org_data.slug).first()

    if not organization:
        country_exists = db.query(models.Country).filter_by(id=org_data.country_id).first()
        if not country_exists:
            return BaseController.errorGeneral(messages[selected_language]['country_not_found'], status.HTTP_404_NOT_FOUND)

        free_trial_days = str(org_data.free_trial_days or '0').upper()
        if free_trial_days not in ('0', '1W', '2W', '1M'):
            return BaseController.errorGeneral(
                'free_trial_days must be one of 0, 1W, 2W, 1M', status.HTTP_400_BAD_REQUEST,
            )
        organization = models.Organization(
            slug=org_data.slug,
            country_id=org_data.country_id,
            is_active=org_data.is_active,
            free_trial_days=free_trial_days,
        )
        db.add(organization)
        db.commit()
        db.refresh(organization)
        if org_data.member_discounts:
            from app.utils.org_membership import upsert_member_discount_matrix
            upsert_member_discount_matrix(db, organization.id, org_data.member_discounts)
            db.commit()
        
        for trans in org_data.translations:
            translation = models.OrganizationTranslation(
                organization_id=organization.id,
                language_id=trans.language_id,
                org_name=trans.org_name
            )
            db.add(translation)

        db.commit()
        return BaseController.success([],messages[selected_lang]['org_added'])
    else:
        return BaseController.errorGeneral(messages[selected_language]['org_already_exist'], status.HTTP_208_ALREADY_REPORTED)

@router.put("/organization/{org_id}", dependencies=[Depends(admin_required)])
async def update_organization(
    org_id: int,
    org_data: admin.OrganizationUpdate,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en')
):
    organization = db.query(models.Organization).filter_by(id=org_id).first()

    if not organization:
        return BaseController.errorGeneral(messages[selected_language]['organization_not_found'], status.HTTP_404_NOT_FOUND)
    else:
        previous_is_active = organization.is_active
        organization.country_id = org_data.country_id
        organization.is_active = org_data.is_active
        organization.has_document = org_data.has_document
        organization.responsible_person_name = org_data.responsible_person_name
        organization.responsible_person_email = org_data.responsible_person_email
        if org_data.free_trial_days is not None:
            normalized_trial = str(org_data.free_trial_days).upper()
            if normalized_trial not in ('0', '1W', '2W', '1M'):
                return BaseController.errorGeneral(
                    'free_trial_days must be one of 0, 1W, 2W, 1M', status.HTTP_400_BAD_REQUEST,
                )
            organization.free_trial_days = normalized_trial
        if org_data.member_discounts is not None:
            from app.utils.org_membership import upsert_member_discount_matrix
            upsert_member_discount_matrix(db, organization.id, org_data.member_discounts)
        if previous_is_active == '1' and org_data.is_active == '0':
            from app.utils.org_membership import cascade_deactivate_organization
            token_data = verify_access_token(token)
            actor = db.query(models.User).filter(models.User.email == token_data.email).first()
            cascade_deactivate_organization(
                db,
                organization_id=organization.id,
                actor_id=actor.id if actor else None,
            )
        db.commit()
        
        existing_translations = db.query(models.OrganizationTranslation).filter_by(organization_id=org_id).all()
        existing_translation_map = {trans.language_id: trans for trans in existing_translations}

        new_translation_language_ids = {trans.language_id for trans in org_data.translations}
        for trans in org_data.translations:
            if trans.language_id in existing_translation_map:
                existing_translation = existing_translation_map[trans.language_id]
                existing_translation.org_name = trans.org_name
            else:
                new_translation = models.OrganizationTranslation(
                    organization_id=org_id,
                    language_id=trans.language_id,
                    org_name=trans.org_name
                )
                db.add(new_translation)

        for existing_translation in existing_translations:
            if existing_translation.language_id not in new_translation_language_ids:
                db.delete(existing_translation)

        db.commit()
            
        return BaseController.success([],messages[selected_lang]['org_updated'])


@router.patch("/organization/{org_id}/status", dependencies=[Depends(admin_required)])
async def update_organization_status(
    org_id: int,
    status_data: admin.OrganizationStatusUpdate,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str]=Cookie(default='en')
):
    organization = db.query(models.Organization).filter_by(id=org_id).first()

    if not organization:
        return BaseController.errorGeneral(messages[selected_language]['organization_not_found'], status.HTTP_404_NOT_FOUND)
    previous_is_active = organization.is_active
    if status_data.is_active:
        organization.is_active = status_data.is_active
    if status_data.has_document:
        if status_data.has_document == "1":
            organization.has_document = True
        else:
            organization.has_document = False
    if previous_is_active == '1' and status_data.is_active == '0':
        from app.utils.org_membership import cascade_deactivate_organization
        token_data = verify_access_token(token)
        actor = db.query(models.User).filter(models.User.email == token_data.email).first()
        cascade_deactivate_organization(
            db,
            organization_id=organization.id,
            actor_id=actor.id if actor else None,
        )
    db.commit()

    return BaseController.success([],messages[selected_lang]['org_updated'])

# ──────────────────────────────────────────────
# Booking Disputes
# ──────────────────────────────────────────────
@router.get("/booking-disputes", dependencies=[Depends(admin_required)])
def get_booking_disputes(
    filters: admin.BookingsDisputeAdminFilter = Depends(),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
    page: int = Query(default=0, ge=0, description="Page number for pagination"),
    limit: int = Query(default=10, ge=1, le=100, description="Number of bookings to return per page")
):
    # Query the BookingDispute model
    query = db.query(models.BookingDispute)
    booking_disputes = query.options(
        joinedload(models.BookingDispute.booking),
        joinedload(models.BookingDispute.user),
        joinedload(models.BookingDispute.dispute_type),
        joinedload(models.BookingDispute.user1)
    )
        
    if filters.dispute_status is not None and filters.dispute_status != '' and filters.dispute_status != "all":
        booking_disputes = booking_disputes.filter(
            models.BookingDispute.dispute_status == filters.dispute_status
        )

    if filters.payment_status is not None and filters.payment_status != '' and filters.payment_status != "all":
        booking_disputes = booking_disputes.filter(
            models.BookingDispute.booking.has(
                models.Booking.transactions.any(
                    models.BookingTransaction.trx_status == filters.payment_status
                )
            )
        ) 

    if filters.search is not None and filters.search != '':
        search_terms = filters.search.split()
        search_filters = []
        for term in search_terms:
            like = f"%{term}%"
            search_filters.append(
                or_(
                    models.BookingDispute.dispute_reason.ilike(like),
                    models.BookingDispute.booking.has(
                        or_(
                            models.Booking.order_number.ilike(like),
                            models.Booking.customer_name.ilike(like),
                            models.Booking.supplier_name.ilike(like),
                        )
                    ),
                )
            )
        booking_disputes = booking_disputes.filter(*search_filters)

    if filters.dispute_from_date or filters.dispute_to_date:
        dispute_filters = []
        if filters.dispute_from_date:
            dispute_filters.append(cast(models.BookingDispute.created_at, Date) >= convert_date(filters.dispute_from_date))
        if filters.dispute_to_date:
            dispute_filters.append(cast(models.BookingDispute.created_at, Date) <= convert_date(filters.dispute_to_date))
        
        if dispute_filters:
            booking_disputes = booking_disputes.filter(and_(*dispute_filters))        
    
    total_records = booking_disputes.count()
    
    booking_dispute_records = booking_disputes.order_by(desc(models.BookingDispute.id)).offset(page * limit).limit(limit).all()
    
    if not booking_dispute_records:
        return BaseController.errorGeneral(messages[selected_language]['booking_dispute_not_found'], status.HTTP_404_NOT_FOUND)

    language_id = (
        db.query(models.Language.id)
        .filter(models.Language.code == selected_language)
        .scalar() or 1
    )
    dispute_type_ids = {d.dispute_type_id for d in booking_dispute_records if d.dispute_type_id}
    dispute_type_trans_map = {}
    if dispute_type_ids:
        dispute_type_trans_map = {
            t.id: t.dispute_title
            for t in db.query(models.DisputeTypeTranslation).filter(
                models.DisputeTypeTranslation.id.in_(dispute_type_ids),
                models.DisputeTypeTranslation.language_id == language_id,
            ).all()
        }

    disputes_data = []

    for dispute in booking_dispute_records:
        # Fetch and serialize transaction info
        transaction = (
            db.query(models.transactions.BookingTransaction)
            .filter(models.transactions.BookingTransaction.booking_id == dispute.booking.id)
            .order_by(desc(models.transactions.BookingTransaction.id))
            .first()
        )
        payment_intent = jsonable_encoder(transaction.payment_intent) if transaction else None

        dispute_type_info = jsonable_encoder(dispute.dispute_type)
        slug = dispute_type_trans_map.get(dispute.dispute_type_id)
        if dispute_type_info is not None and slug:
            dispute_type_info['slug'] = slug

        # Build dispute data
        dispute_data = {
            "id": dispute.id,
            "dispute_reason": dispute.dispute_reason,
            "dispute_img": dispute.dispute_img,
            "dispute_status": dispute.dispute_status,
            "reaction_reason": dispute.reaction_reason,
            "created_at": dispute.created_at,
            "updated_at": dispute.updated_at,
            "dispute_type": dispute_type_info,
            "created_by": {
                "id": dispute.user.id,
                "first_name": decrypt_data(dispute.user.first_name),
                "last_name": decrypt_data(dispute.user.last_name),
                "company_name": decrypt_data(dispute.user.company_name),
                "email": dispute.user.email,
            },
            "reacted_by": {
                "id": dispute.user1.id,
                "first_name": decrypt_data(dispute.user1.first_name),
                "last_name": decrypt_data(dispute.user1.last_name),
                "company_name": decrypt_data(dispute.user1.company_name),
                "email": dispute.user1.email,
            } if dispute.user1 else None,
            "booking": {
                "id": dispute.booking.id,
                "order_number": dispute.booking.order_number,
                "currency_code": dispute.booking.currency_code,
                "currency_symbol": dispute.booking.currency_symbol,
                "created_at": dispute.booking.created_at,
                "total_price": compute_booking_total(dispute.booking),
                "supplier_name": dispute.booking.supplier_name,
                "supplier_company_name": decrypt_data(dispute.booking.supplier.user.company_name) if dispute.booking.supplier and dispute.booking.supplier.user else None,
                "customer_name": dispute.booking.customer_name,
                "customer_company_name": decrypt_data(dispute.booking.user1.company_name) if dispute.booking.user1 else None,
                "payment_mode": dispute.booking.payment_mode,
                "status": dispute.booking.status,
                "payment_intent": payment_intent,
                "payment_status": transaction.trx_status if transaction else None
            },
        }

        # Add to disputes_data
        disputes_data.append(jsonable_encoder(dispute_data))
    
    return PaginationResponse(
            total=total_records,
            page=page,
            per_page=limit,
            data=disputes_data,
            message=messages[selected_language]['booking_disputes_list'])

@router.put("/booking_dispute/{booking_dispute_id}/status", dependencies=[Depends(admin_required)])
async def change_booking_dispute_status(
    booking_dispute_id: int,
    form_data: admin.updateBookingDisputeStatus,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en')
):
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    
    booking_dispute = db.query(models.BookingDispute).filter_by(id=booking_dispute_id).first()
    if not booking_dispute:
        return BaseController.errorGeneral(messages[selected_language]['booking_dispute_not_found'], status.HTTP_404_NOT_FOUND)
    
    if form_data.dispute_status not in ['0', '1']:
        return BaseController.errorGeneral(messages[selected_language]['something_went_wrong'])
    
    booking_dispute.dispute_status = form_data.dispute_status
    booking_dispute.reaction_reason = form_data.reaction_reason
    booking_dispute.reacted_by= user.id
    booking_dispute.updated_at = datetime.now()
    
    db.commit()
    
    booking = (db.query(models.Booking)
        .filter(models.Booking.id == booking_dispute.booking_id)
        .options(joinedload(models.Booking.supplier).joinedload(models.Supplier.user))
        .options(joinedload(models.Booking.user)).first())
    if booking:
        if booking_dispute.dispute_status == '1':
            booking.status = 'complete_closed'
        else:    
            booking.status = 'dispute'
        
        booking.updated_at = datetime.now()
        
        db.commit()
        customer_template_content = get_email_template_content(db, booking.user1.language_id, 'order-updated')
        supplier_template_content = get_email_template_content(db, booking.supplier.user.language_id, 'order-updated')
        customer_payment_status = messages[booking.user1.language.code]["offline_booking"]
        supplier_payment_status = messages[booking.supplier.user.language.code]["offline_booking"]
        if booking.payment_mode == "online":
            customer_payment_status = messages[booking.user1.language.code]["online_booking"]
            supplier_payment_status = messages[booking.supplier.user.language.code]["online_booking"]
        if customer_template_content and booking.user1.send_mail_notification == "0":
            background_tasks.add_task(
                send_order_email,
                email=booking.user1.email,
                order_number=booking.order_number,
                subject=customer_template_content.subject,
                body=customer_template_content.body,
                first_name=decrypt_data(booking.user1.first_name),
                last_name=decrypt_data(booking.user1.last_name),
                status=messages[booking.user1.language.code][booking.status],
                booking_id=booking.order_number,
                customer_name=booking.customer_name,
                service_provider_name=booking.supplier_name,
                price=f"{booking.supplier.user.currency.symbol}{str(booking.total_price)}",
                booking_date=(booking.order_date_time).strftime("%d/%m/%Y"),
                booking_time=(booking.order_date_time).strftime("%H:%M"),
                payment_mode=customer_payment_status,
                selected_language=booking.user1.language.code
            )
        if supplier_template_content and booking.supplier.user.send_mail_notification == "0":
            background_tasks.add_task(
                send_order_email,
                email=booking.supplier.user.email,
                order_number=booking.order_number,
                subject=supplier_template_content.subject,
                body=supplier_template_content.body,
                first_name=decrypt_data(booking.supplier.user.first_name),
                last_name=decrypt_data(booking.supplier.user.last_name),
                status=messages[booking.supplier.user.language.code][booking.status],
                booking_id=booking.order_number,
                customer_name=booking.customer_name,
                service_provider_name=booking.supplier_name,
                price=f"{booking.supplier.user.currency.symbol}{str(booking.total_price)}",
                booking_date=(booking.order_date_time).strftime("%d/%m/%Y"),
                booking_time=(booking.order_date_time).strftime("%H:%M"),
                payment_mode=supplier_payment_status,
                selected_language=booking.supplier.user.language.code
            )
    
    
    return BaseController.success([], messages[selected_lang]['booking_dispute_updated'])    

# ──────────────────────────────────────────────
# Dashboard & Analytics
# ──────────────────────────────────────────────
@router.get("/dashboard/stats", response_model=admin.StatisticsResponse, summary="Get dashboard statistics", dependencies=[Depends(admin_required)])
def get_dashboard_statistics(
    db: Session = Depends(get_db),
    selected_language: Optional[str]=Cookie(default='en')
    ):
    try:
        total_users = db.query(models.User).filter(models.User.role_id == 2).count()
        approved_users = db.query(models.User).filter(models.User.role_id == 2, models.User.status == 'Approved').count()
        total_suppliers = db.query(models.Supplier).count()
        approved_suppliers = db.query(models.Supplier).filter(models.Supplier.status == 'Approved').count()
        total_bookings = db.query(models.Booking).count()
        total_completed_bookings = db.query(models.Booking).filter(models.Booking.status == 'complete_closed').count()
        total_amount = db.query(models.Booking).with_entities(
            func.sum(models.Booking.total_price)
        ).filter(models.Booking.status == 'complete_closed').scalar() or 0.00

        # Fetch sales overview for the last 6 months
        six_months_ago = datetime.now() - timedelta(days=180)  # Approximate 6 months
        bookings_data = db.query(
            func.date_format(models.Booking.created_at, "%b").label("month"),
            func.sum(
                case((models.Booking.payment_mode == "offline", 1), else_=0)
            ).label("offline_bookings"),
            func.sum(
                case((models.Booking.payment_mode == "online", 1), else_=0)
            ).label("online_bookings")
        ).filter(
            models.Booking.created_at >= six_months_ago
        ).group_by(
            func.date_format(models.Booking.created_at, "%b"),
            extract("month", models.Booking.created_at)
        ).order_by(
            extract("month", models.Booking.created_at)
        ).all()
        months_list = [
            (datetime.now() - timedelta(days=i * 30)).strftime('%b')
            for i in range(5, -1, -1)  # Last 6 months from today
        ]

        # Prepare sales overview data
        sales_overview = {
            "months": [],
            "offline_bookings": [],
            "online_bookings": []
        }
        sales_overview["months"] = months_list
        sales_overview["offline_bookings"] = [0] * 6
        sales_overview["online_bookings"] = [0] * 6

        for data in bookings_data:
            try:
                month_name = data.month
                month_index = months_list.index(month_name)
                sales_overview["offline_bookings"][month_index] = str(data.offline_bookings)
                sales_overview["online_bookings"][month_index] = str(data.online_bookings)
            except Exception:
                logger.exception("Error assembling sales-overview month row")
                continue

        result =  admin.StatisticsResponse(
            total_users=total_users,
            approved_users=approved_users,
            total_suppliers=total_suppliers,
            approved_suppliers = approved_suppliers,
            total_bookings=total_bookings,
            total_completed_bookings=total_completed_bookings,
            total_amount=str(Decimal(total_amount).quantize(Decimal("0.00"))),
            sales_overview=sales_overview
        )
        return BaseController.success([result], messages[selected_lang]['statistics_details'])    
        
    except Exception as e:
        return BaseController.errorGeneral(messages[selected_language]['something_went_wrong'])

@router.get("/booking-disputes/{booking_dispute_id}", dependencies=[Depends(admin_required)])
def get_booking_disputes(
    booking_dispute_id: int,
    db: Session = Depends(get_db),
    selected_language: Optional[str]=Cookie(default='en')
):
    language_id = (
            db.query(models.Language.id)
            .filter(models.Language.code == selected_language)
            .scalar() or 1
        )
    
    # Query the BookingDispute model
    query = db.query(models.BookingDispute).filter(models.BookingDispute.id == booking_dispute_id)
    booking_disputes = query.options(
        joinedload(models.BookingDispute.booking),
        joinedload(models.BookingDispute.user),
        joinedload(models.BookingDispute.dispute_type),
        joinedload(models.BookingDispute.user1)
    ).first()
    
    if not booking_disputes:
        return BaseController.errorGeneral(messages[selected_language]['booking_dispute_not_found'], status.HTTP_404_NOT_FOUND)
    dispute_type_trans = db.query(models.DisputeTypeTranslation).filter(models.DisputeTypeTranslation.id == booking_disputes.dispute_type_id, models.DisputeTypeTranslation.language_id == language_id).first()
    
    dispute_type_info = jsonable_encoder(booking_disputes.dispute_type)

    if dispute_type_trans:
        dispute_type_info['slug'] = dispute_type_trans.dispute_title
    
    transaction = (
        db.query(models.transactions.BookingTransaction)
        .filter(models.transactions.BookingTransaction.booking_id == booking_disputes.booking.id)
        .order_by(desc(models.transactions.BookingTransaction.id))
        .first()
    )
    payment_intent = jsonable_encoder(transaction.payment_intent) if transaction else None    
    
    conversation = (
        db.query(models.Conversations)
        .filter(models.Conversations.booking_id == booking_disputes.booking.id)
        .order_by(desc(models.Conversations.id))
        .first()
    )
    conversation_id = jsonable_encoder(conversation.id) if conversation else None    
    
    disputes_data = {
            "id": booking_disputes.id,
            "dispute_reason": booking_disputes.dispute_reason,
            "dispute_img": booking_disputes.dispute_img,
            "dispute_status": booking_disputes.dispute_status,            
            "reaction_reason": booking_disputes.reaction_reason,
            "created_at": booking_disputes.created_at,
            "updated_at": booking_disputes.updated_at,
            "dispute_type": dispute_type_info,
            "conversation_id":conversation_id,
            "created_by": {
                "id":booking_disputes.user.id,
                "first_name":decrypt_data(booking_disputes.user.first_name),
                "last_name":decrypt_data(booking_disputes.user.last_name),
                "company_name":decrypt_data(booking_disputes.user.company_name),
                "email":booking_disputes.user.email
                },
            "reacted_by": {
                "id":booking_disputes.user.id,
                "first_name":decrypt_data(booking_disputes.user.first_name),
                "last_name":decrypt_data(booking_disputes.user.last_name),
                "company_name":decrypt_data(booking_disputes.user.company_name),
                "email":booking_disputes.user.email
                } if booking_disputes.user1 else None,
            "booking":{
                "id": booking_disputes.booking.id,
                "order_number": booking_disputes.booking.order_number,
                "currency_code": booking_disputes.booking.currency_code,
                "currency_symbol": booking_disputes.booking.currency_symbol,
                "created_at": booking_disputes.booking.created_at,
                "total_price": compute_booking_total(booking_disputes.booking),
                "base_price": float(booking_disputes.booking.total_price or 0.0),
                "additional_fee": float(booking_disputes.booking.additional_fee or 0.0),
                "discount": float(booking_disputes.booking.discount or 0.0),
                "supplier_name": booking_disputes.booking.supplier_name,
                "supplier_company_name": decrypt_data(booking_disputes.booking.supplier.user.company_name) if booking_disputes.booking.supplier and booking_disputes.booking.supplier.user else None,
                "customer_name": booking_disputes.booking.customer_name,
                "customer_company_name": decrypt_data(booking_disputes.booking.user1.company_name) if booking_disputes.booking.user1 else None,
                "payment_mode": booking_disputes.booking.payment_mode,
                "status": booking_disputes.booking.status,
                "payment_intent": payment_intent,
                "payment_status": transaction.trx_status if transaction else None,
                "is_sct": bool(transaction.is_sct) if transaction else False,
                "transfer_status": transaction.transfer_status if transaction else None,
                "charged_amount": float(transaction.amount) if transaction and transaction.amount is not None else 0.0,
                "refunded_amount": float(transaction.refunded_amount) if transaction and transaction.refunded_amount is not None else 0.0,
                },
        }
    return BaseController.success(jsonable_encoder(disputes_data), messages[selected_lang]['booking_disputes_list'])

# ──────────────────────────────────────────────
# Countries
# ──────────────────────────────────────────────
@router.get("/countries", dependencies=[Depends(admin_required)])
def get_countries(
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    language_id = (
            db.query(models.Language.id)
            .filter(models.Language.code == selected_language)
            .scalar() or 1
        )
    if not language_id:
        return BaseController.errorGeneral("Selected language not supported", 400)
    country_list = db.query(models.Country).options(joinedload(models.Country.translations)).all()

    if country_list:
        response_data = [
            common.GetAdminCountries(
                id=country.id,
                country_name=next(
                    (t.country_name for t in country.translations if t.language_id == language_id),
                    country.country_name
                ),
                country_code=country.country_code,
                region_id=country.region_id,
                region_name=country.region.region_name,
                status=country.status,
                created_at=country.created_at,
                updated_at=country.updated_at
            )
            for country in country_list
        ]
        return BaseController.success(jsonable_encoder(response_data), messages[selected_language]['country_list'])
    else:
        return BaseController.errorGeneral(messages[selected_language]['country_not_found'], 400)


@router.post("/add_country", dependencies=[Depends(admin_required)])
async def create_country(
    country_name: str = Form(...),
    country_code: str = Form(...),
    region_id: int = Form(...),
    country_icon: Union[UploadFile, str, None] = File(None),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    country_exist = db.query(models.Country).filter(models.Country.country_name == country_name).first()
    if country_exist:
        return BaseController.errorGeneral(messages[selected_language]['country_exist'], 400)
    
    icon = None
    if country_icon and not isinstance(country_icon, str):
        if not country_icon.content_type.startswith("image/"):
            return BaseController.errorGeneral(
                messages[selected_language]['invalid_file_format'], 400)
        
        if not allowed_file(country_icon.filename):
            return BaseController.errorGeneral(messages[selected_language]['invalid_extension'], 400)
        
        if not os.path.exists(os.path.join(settings.FILE_DIR_PATH, COUNTRY_UPLOAD_DIR)):
            os.makedirs(os.path.join(settings.FILE_DIR_PATH, COUNTRY_UPLOAD_DIR))

        file_extension = country_icon.filename.split(".")[-1]
        file_name = f"{country_name.strip().replace(' ', '_')}_{str(uuid.uuid4())}.{file_extension}"
        file_path = os.path.join(COUNTRY_UPLOAD_DIR, file_name)

        with open(os.path.join(settings.FILE_DIR_PATH, file_path), "wb") as f:
            f.write(await country_icon.read())
        
        icon = file_path
    
    country_info = models.Country(country_name=country_name, country_code = country_code, region_id = region_id, country_icon = icon)
    db.add(country_info)
    db.commit()
    db.refresh(country_info)
    
    return BaseController.success(jsonable_encoder(country_info),messages[selected_lang]['country_created'])


@router.post("/country/{country_id}/translation", dependencies=[Depends(admin_required)])
async def add_country_translation(
    country_id: int,
    country_name: str = Form(...),
    language_id: int = Form(...),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    country_exists = db.query(models.Country).filter_by(id=country_id).first()
    language_exists = db.query(models.Language).filter_by(id=language_id).first()

    if not country_exists:
        return BaseController.errorGeneral(messages[selected_language]['country_not_found'], 404)
    if not language_exists:
        return BaseController.errorGeneral(messages[selected_language]['language_not_found'], 404)

    new_translation = models.CountryTranslation(
        country_id=country_id,
        language_id=language_id,
        country_name=country_name
    )

    db.add(new_translation)
    db.commit()

    return BaseController.success([],messages[selected_lang]['country_created'])


@router.put("/update_country/{country_id}", dependencies=[Depends(admin_required)])
async def update_country(
    country_id: int,
    country_name: Optional[str] = Form(None),
    country_code: Optional[str] = Form(None),
    region_id: Optional[int] = Form(None),
    status: Optional[str] = Form(None),
    country_icon: Union[UploadFile, str, None] = File(None),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    country = db.query(models.Country).filter(models.Country.id == country_id).first()
    if not country:
        return BaseController.errorGeneral(messages[selected_language]['country_not_found'], 400)
    if country_icon and not isinstance(country_icon, str):
        if not country_icon.content_type.startswith("image/"):
            return BaseController.errorGeneral(
                messages[selected_language]['invalid_file_format'], 400)
        
        if not allowed_file(country_icon.filename):
            return BaseController.errorGeneral(messages[selected_language]['invalid_extension'], 400)
        
        if not os.path.exists(os.path.join(settings.FILE_DIR_PATH, COUNTRY_UPLOAD_DIR)):
            os.makedirs(os.path.join(settings.FILE_DIR_PATH, COUNTRY_UPLOAD_DIR))

        file_extension = country_icon.filename.split(".")[-1]
        file_name = f"{country_name.strip().replace(' ', '_')}_{str(uuid.uuid4())}.{file_extension}"
        file_path = os.path.join(COUNTRY_UPLOAD_DIR, file_name)

        with open(os.path.join(settings.FILE_DIR_PATH, file_path), "wb") as f:
            f.write(await country_icon.read())
        
        country.country_icon = file_path
    if country_name is not None:
        country.country_name = country_name
    if country_code is not None:
        country.country_code = country_code
    if region_id is not None:
        country.region_id = region_id
    if status is not None:
        country.status = status
    db.commit()
    
    return BaseController.success([], messages[selected_language]['country_updated'])


@router.get("/country/{country_id}/translation", dependencies=[Depends(admin_required)])
def get_country_translations(
    country_id: int,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    country_data = db.query(models.Country).filter(models.Country.id == country_id).first()
    if not country_data:
        return BaseController.errorGeneral(messages[selected_language]['country_not_found'], status.HTTP_404_NOT_FOUND)
    
    country_trans_list = db.query(models.CountryTranslation).options(joinedload(models.CountryTranslation.language)).filter(models.CountryTranslation.country_id == country_id).all()
    
    if country_trans_list:
        country_info = [
            admin.CountryTranslation(
                id=country_trans.id,
                country_id=country_trans.country_id,
                language_id=country_trans.language_id,
                language_name=country_trans.language.name,
                language_code=country_trans.language.code,
                country_name=country_trans.country_name,
                created_at=country_trans.created_at,
                updated_at=country_trans.updated_at if country_trans.updated_at else None,
            )
            for country_trans in country_trans_list
        ]
        combined_response = {
            "category": jsonable_encoder(country_data),
            "translations": jsonable_encoder(country_info)
        }
        
        return BaseController.success(combined_response, messages[selected_language]['country_list'])
    else:
        return BaseController.errorGeneral(messages[selected_language]['country_not_found'], 404)


@router.put("/country/{country_id}/upd_trans", dependencies=[Depends(admin_required)])
async def update_country_translation(
    country_id: int,
    language_id: int,
    country_name: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    country_exists = db.query(models.Country).filter_by(id=country_id).first()
    language_exists = db.query(models.Language).filter_by(id=language_id).first()
    if not country_exists:
        return BaseController.errorGeneral(messages[selected_language]['country_not_found'], 404)
    if not language_exists:
        return BaseController.errorGeneral(messages[selected_language]['language_not_found'], 404)

    translation = (
        db.query(models.CountryTranslation)
        .filter_by(country_id=country_id, language_id=language_id)
        .first()
    )
    
    if not translation:
        return BaseController.errorGeneral(messages[selected_language]['translation_not_found'], 404)

    if country_name:
        translation.country_name = country_name
        db.commit()

    return BaseController.success([], messages[selected_language]['country_updated'])


# ──────────────────────────────────────────────
# Nationalities
# ──────────────────────────────────────────────
@router.get("/nationalities", dependencies=[Depends(admin_required)])
def get_nationalities(
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    language = db.query(models.Language).filter(models.Language.code == selected_language).first()
    if not language:
        return BaseController.errorGeneral("Selected language not supported", 400)
    nationality_list = db.query(models.Nationality).options(joinedload(models.Nationality.translations)).all()
    if nationality_list:
        nationality_response = [
            common.GetAdminCountries(
                id=nationality.id,
                country_name=next(
                    (t.country_name for t in nationality.translations if t.language_id == language.id),
                    nationality.country_name
                ),
                country_code=nationality.country_code,
                region_id=nationality.region_id,
                region_name=nationality.region.region_name,
                status=nationality.status,
                created_at=nationality.created_at,
                updated_at=nationality.updated_at
            )
            for nationality in nationality_list
        ]
        return BaseController.success(jsonable_encoder(nationality_response), messages[selected_language]['nationality_list'])
    else:
        return BaseController.errorGeneral(messages[selected_language]['nationality_not_found'], 400)


@router.post("/add_nationality", dependencies=[Depends(admin_required)])
async def create_nationality(
    country_name: str = Form(...),
    country_code: str = Form(...),
    region_id: int = Form(...),
    country_icon: Union[UploadFile, str, None] = File(None),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    nationality_exist = db.query(models.Nationality).filter(models.Nationality.country_name == country_name).first()
    if nationality_exist:
        return BaseController.errorGeneral(messages[selected_language]['nationality_exist'], 400)
    
    icon = None
    if country_icon and not isinstance(country_icon, str):
        if not country_icon.content_type.startswith("image/"):
            return BaseController.errorGeneral(
                messages[selected_language]['invalid_file_format'], 400)
        
        if not allowed_file(country_icon.filename):
            return BaseController.errorGeneral(messages[selected_language]['invalid_extension'], 400)
        
        if not os.path.exists(os.path.join(settings.FILE_DIR_PATH, NATIONALITY_UPLOAD_DIR)):
            os.makedirs(os.path.join(settings.FILE_DIR_PATH, NATIONALITY_UPLOAD_DIR))

        file_extension = country_icon.filename.split(".")[-1]
        file_name = f"{country_name.strip().replace(' ', '_')}_{str(uuid.uuid4())}.{file_extension}"
        file_path = os.path.join(NATIONALITY_UPLOAD_DIR, file_name)

        with open(os.path.join(settings.FILE_DIR_PATH, file_path), "wb") as f:
            f.write(await country_icon.read())
        
        icon = file_path
    
    nationality_info = models.Nationality(country_name=country_name, country_code = country_code, region_id = region_id, country_icon = icon)
    db.add(nationality_info)
    db.commit()
    db.refresh(nationality_info)
    
    return BaseController.success(jsonable_encoder(nationality_info),messages[selected_lang]['nationality_created'])


@router.post("/nationality/{nationality_id}/translation", dependencies=[Depends(admin_required)])
async def add_nationality_translation(
    nationality_id: int,
    country_name: str = Form(...),
    language_id: int = Form(...),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    nationality_exists = db.query(models.Nationality).filter_by(id=nationality_id).first()
    language_exists = db.query(models.Language).filter_by(id=language_id).first()

    if not nationality_exists:
        return BaseController.errorGeneral(messages[selected_language]['nationality_not_found'], 404)
    if not language_exists:
        return BaseController.errorGeneral(messages[selected_language]['language_not_found'], 404)

    new_translation = models.NationalityTranslation(
        nationality_id=nationality_id,
        language_id=language_id,
        country_name=country_name
    )

    db.add(new_translation)
    db.commit()

    return BaseController.success([],messages[selected_lang]['nationality_created'])


@router.put("/update_nationality/{nationality_id}", dependencies=[Depends(admin_required)])
async def update_nationality(
    nationality_id: int,
    country_name: Optional[str] = Form(None),
    country_code: Optional[str] = Form(None),
    region_id: Optional[int] = Form(None),
    status: Optional[str] = Form(None),
    country_icon: Union[UploadFile, str, None] = File(None),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    nationality = db.query(models.Nationality).filter(models.Nationality.id == nationality_id).first()
    if not nationality:
        return BaseController.errorGeneral(messages[selected_language]['nationality_not_found'], 400)
    if country_icon and not isinstance(country_icon, str):
        if not country_icon.content_type.startswith("image/"):
            return BaseController.errorGeneral(
                messages[selected_language]['invalid_file_format'], 400)
        
        if not allowed_file(country_icon.filename):
            return BaseController.errorGeneral(messages[selected_language]['invalid_extension'], 400)
        
        if not os.path.exists(os.path.join(settings.FILE_DIR_PATH, NATIONALITY_UPLOAD_DIR)):
            os.makedirs(os.path.join(settings.FILE_DIR_PATH, NATIONALITY_UPLOAD_DIR))

        file_extension = country_icon.filename.split(".")[-1]
        file_name = f"{country_name.strip().replace(' ', '_')}_{str(uuid.uuid4())}.{file_extension}"
        file_path = os.path.join(NATIONALITY_UPLOAD_DIR, file_name)

        with open(os.path.join(settings.FILE_DIR_PATH, file_path), "wb") as f:
            f.write(await country_icon.read())
        
        nationality.country_icon = file_path
    if country_name is not None:
        nationality.country_name = country_name
    if country_code is not None:
        nationality.country_code = country_code
    if region_id is not None:
        nationality.region_id = region_id
    if status is not None:
        nationality.status = status
    db.commit()
    
    return BaseController.success([], messages[selected_language]['nationality_updated'])


@router.get("/nationality/{nationality_id}/translation", dependencies=[Depends(admin_required)])
def get_nationality_translations(
    nationality_id: int,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    nationality_data = db.query(models.Nationality).filter(models.Nationality.id == nationality_id).first()
    if not nationality_data:
        return BaseController.errorGeneral(messages[selected_language]['nationality_not_found'], status.HTTP_404_NOT_FOUND)
    
    nationality_trans_list = db.query(models.NationalityTranslation).options(joinedload(models.NationalityTranslation.language)).filter(models.NationalityTranslation.nationality_id == nationality_id).all()
    
    if nationality_trans_list:
        nationality_info = [
            admin.NationalityTranslation(
                id=nationality_trans.id,
                nationality_id=nationality_trans.nationality_id,
                language_id=nationality_trans.language_id,
                language_name=nationality_trans.language.name,
                language_code=nationality_trans.language.code,
                country_name=nationality_trans.country_name,
                created_at=nationality_trans.created_at,
                updated_at=nationality_trans.updated_at if nationality_trans.updated_at else None,
            )
            for nationality_trans in nationality_trans_list
        ]
        combined_response = {
            "category": jsonable_encoder(nationality_data),
            "translations": jsonable_encoder(nationality_info)
        }
        
        return BaseController.success(combined_response, messages[selected_language]['nationality_list'])
    else:
        return BaseController.errorGeneral(messages[selected_language]['nationality_not_found'], 404)


@router.put("/nationality/{nationality_id}/upd_trans", dependencies=[Depends(admin_required)])
async def update_country_translation(
    nationality_id: int,
    language_id: int,
    country_name: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    nationality_exists = db.query(models.Nationality).filter_by(id=nationality_id).first()
    language_exists = db.query(models.Language).filter_by(id=language_id).first()
    if not nationality_exists:
        return BaseController.errorGeneral(messages[selected_language]['nationality_not_found'], 404)
    if not language_exists:
        return BaseController.errorGeneral(messages[selected_language]['language_not_found'], 404)

    translation = (
        db.query(models.NationalityTranslation)
        .filter_by(nationality_id=nationality_id, language_id=language_id)
        .first()
    )
    
    if not translation:
        return BaseController.errorGeneral(messages[selected_language]['translation_not_found'], 404)

    if country_name:
        translation.country_name = country_name
        db.commit()

    return BaseController.success([], messages[selected_language]['nationality_updated'])

# ──────────────────────────────────────────────
# Chats
# ──────────────────────────────────────────────
@router.get("/chats", dependencies=[Depends(admin_required)])
async def get_chats(
    search_term: Optional[str] = Query(None, description="Search for user name by keyword"),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
    limit: int = Query(default=10, ge=1, le=100, description=""),
    page: int = Query(default=0, ge=0, description="")
):
    conversation_query = (
        db.query(models.Conversations)
        .join(models.ConversationParticipants, models.Conversations.id == models.ConversationParticipants.conversation_id)
        .filter(models.Conversations.chat_type == 'normal')
        .group_by(models.Conversations.id)
        .order_by(desc(models.Conversations.id))
    )
    
    total_conversation = conversation_query.count()
    
    conversations = conversation_query.offset(page * limit).limit(limit).all()
    response_data = []
    for conversation in conversations:
        participant_query = (
            db.query(models.User)
            .join(models.ConversationParticipants, models.User.id == models.ConversationParticipants.participant_id)
            .join(models.Role, models.Role.id == models.User.role_id)
            .filter(models.ConversationParticipants.conversation_id == conversation.id)
            .filter(models.Role.guard_name != 'admin')
        )
        if search_term:
            full_name_hash_strings = []
            keyword_list = search_term.split()
            for i in range(1, len(keyword_list)):
                first_name_str = " ".join(keyword_list[:i])
                last_name_str = " ".join(keyword_list[i:])
                first_name_hash = hash_sort_string(first_name_str)
                last_name_hash = hash_sort_string(last_name_str)
                full_name_hash = f"{first_name_hash} {last_name_hash}"
                full_name_hash_strings.append(full_name_hash)
            participant_query = participant_query.filter(
                or_(
                    models.User.first_name_hash == hash_sort_string(search_term),
                    models.User.last_name_hash == hash_sort_string(search_term),
                    func.concat(models.User.first_name_hash, literal(" "), models.User.last_name_hash).in_(full_name_hash_strings)
                )
            )
        participant = participant_query.all()
        if not participant:
            continue
        participants_list = [f"{decrypt_data(chat_user.first_name)} {decrypt_data(chat_user.last_name)}" for chat_user in participant]
        participants_company_list = [decrypt_data(chat_user.company_name) for chat_user in participant]
        last_message = (
            db.query(models.Messages)
            .filter_by(conversation_id=conversation.id)
            .order_by(models.Messages.created_at.desc())
            .first()
        )
        last_message_data = None
        if last_message:
            last_message_data = {
                "id": last_message.id,
                "conversation_id": last_message.conversation_id,
                "sender_id": last_message.sender_id,
                "body": last_message.body,
                "attach_doc": last_message.attach_doc,
                "created_at": last_message.created_at,
                "message_status": last_message.status
            }
        
        response_data.append(admin.ChatListResponse(
            conversation_id = conversation.id,
            type = conversation.chat_type,
            booking_id = conversation.booking_id,
            order_number = conversation.booking.order_number if conversation.booking else None,
            status = conversation.status,
            created_at = conversation.created_at,
            user_name = participants_list,
            user_company_name = participants_company_list,
            profile_img = participant[0].profile_img,
            user_id = participant[0].id,
            last_message = admin.ChatMessageResponse.model_validate(last_message_data) if last_message_data else None
        ))
        
    return PaginationResponse(
            total=total_conversation,
            page=page,
            per_page=limit,
            data=jsonable_encoder(response_data),
            message=messages[selected_language]['chat_retrieved']
        )

# ──────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────
@router.get("/settings", dependencies=[Depends(admin_required)])
def get_settings(
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    language = db.query(models.Language).filter(models.Language.code == selected_language).first()
    if not language:
        return BaseController.errorGeneral("Selected language not supported", 400)
    
    setting_list = db.query(models.Setting).order_by(asc(models.Setting.key_name)).all()
    
    if setting_list:
        setting = [
            admin.GetAdminSettings(
                id=setting_obj.id,
                key_name=setting_obj.key_name,
                key=setting_obj.key,
                value=setting_obj.value,
                value_type=setting_obj.value_type,
                description=setting_obj.description,
                created_at=setting_obj.created_at,
                updated_at=setting_obj.updated_at
            )
            for setting_obj in setting_list
        ]
        return BaseController.success(jsonable_encoder(setting), messages[selected_language]['settings_list'])
    else:
        return BaseController.errorGeneral(messages[selected_language]['settings_not_found'], 400)

@router.post("/setting", dependencies=[Depends(admin_required)])
async def add_new_setting(
    form_data: admin.AddAdminSettings,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    setting_exist = db.query(models.Setting).filter_by(key_name=form_data.key_name).first()
    if setting_exist:
        return BaseController.errorGeneral(messages[selected_language]['setting_already_exists'], 404)
    new_setting = models.Setting(
        key_name=form_data.key_name,
        key=form_data.key,
        value=form_data.value,
        value_type=form_data.value_type,
        description=form_data.description
    )
    db.add(new_setting)
    db.commit()

    return BaseController.success([],messages[selected_lang]['setting_created'])

@router.put("/setting/{setting_id}", dependencies=[Depends(admin_required)])
async def update_setting(
    setting_id: int,
    form_data: admin.UpdateAdminSettings,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    setting_exit = db.query(models.Setting).filter_by(id=setting_id).first()
    if not setting_exit:
        return BaseController.errorGeneral(messages[selected_language]['setting_not_found'], 404)
    
    setting_exit.key = form_data.key
    setting_exit.value = form_data.value
    setting_exit.value_type = form_data.value_type
    setting_exit.description = form_data.description
    setting_exit.updated_at = datetime.now()
    db.commit()
    db.refresh(setting_exit)

    return BaseController.success(jsonable_encoder(setting_exit), messages[selected_language]['settings_updated'])

@router.put("/supplier_stripe/{supplier_id}", dependencies=[Depends(admin_required)])
async def update_supplier_stripe(
    supplier_id: int,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    supplier = db.query(models.Supplier).filter_by(id=supplier_id).first()
    
    if not supplier:
        return BaseController.errorGeneral(messages[selected_language]['supplier_not_found'], status.HTTP_404_NOT_FOUND)

    if not supplier.stripe_account_id:
         return BaseController.success({}, messages[selected_language]['bank_account_not_added'])

    try:
        account = stripe.Account.retrieve(
            supplier.stripe_account_id,
            api_key=stripe_api_key_for(supplier),
        )
        # Preserve the 'pending' state for suppliers currently under Stripe
        # review — otherwise admins see the same "unverified" label whether
        # onboarding hasn't started or Stripe is 2 hours from approving.
        new_status = map_stripe_requirements_to_status(account.get("requirements"))
        if supplier.stripe_verification_status != new_status:
            supplier.stripe_verification_status = new_status
            supplier.updated_at = datetime.now()
            db.commit()
            db.refresh(supplier)

        return BaseController.success(jsonable_encoder(supplier), messages[selected_language]['bank_details_retrieved'])
    except stripe.error.InvalidRequestError as e:
        return BaseController.errorGeneral(messages[selected_language]['something_wrong'], status.HTTP_404_NOT_FOUND)

@router.get("/chats/{conversation_id}", dependencies=[Depends(admin_required)])
async def get_chat_history(
    conversation_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en')
):
    participants_list = []
    sender_status = 'active'
    conversation = (
        db.query(models.Conversations)
        .join(models.ConversationParticipants, models.Conversations.id == models.ConversationParticipants.conversation_id)
        .filter(models.Conversations.id == conversation_id)
        .first()
    )

    if conversation:
        chats = (
            db.query(models.Messages).join(models.User, models.User.id == models.Messages.sender_id)
            .filter(models.Messages.conversation_id == conversation.id)
            .order_by(models.Messages.created_at.desc())
            .options(joinedload(models.Messages.sender).joinedload(models.User.role))
            .all()
        )
        
        participants = db.query(models.ConversationParticipants).filter_by(conversation_id=conversation.id).all()
        
        for participant in participants:
            participants_list.append(participant.participant_id)
            if participant.participant_id == email_token_verification(token):
                sender_status = participant.status                

        
        chat_history = [
            chat_model.ChatMessageAdminResponse(
                id=chat.id,
                conversation_id=chat.conversation_id,
                sender_id=chat.sender_id,
                sender_first_name=decrypt_data(chat.sender.first_name),
                sender_last_name=decrypt_data(chat.sender.last_name),
                sender_company_name=decrypt_data(chat.sender.company_name) if chat.sender.role.guard_name != "admin" else None,
                sender_profile_img=chat.sender.profile_img,
                body=chat.body,
                message_status=chat.status,
                attach_doc=chat.attach_doc,
                created_at=chat.created_at,
                sender_name="Admin" if chat.sender.role.guard_name == "admin" else f"{decrypt_data(chat.sender.first_name)} {decrypt_data(chat.sender.last_name)}",

            ) for chat in chats
        ]
    chat_response = (
        chat_model.ChatAdminResponse(
            conversation_id=conversation.id,
            conversation_status=conversation.status,
            created_at=conversation.created_at,
            booking_id=conversation.booking_id,
            participants=participants_list,
            sender_status=sender_status,
            messages=chat_history
        )
    )
    return BaseController.success(jsonable_encoder(chat_response), messages[selected_language]['chat_retrieved'])    

@router.post("/send-message/{conversation_id}", dependencies=[Depends(admin_required)])
def add_chat_message(
    conversation_id: int,
    message: chat_model.AddChatMessage,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en')
):
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)
    participants = db.query(models.ConversationParticipants).filter_by(conversation_id=conversation_id, participant_id=user.id).all()
    if not participants:
            participants = [
                models.ConversationParticipants(conversation_id=conversation_id, participant_id=user.id, status='active')
            ]
            db.bulk_save_objects(participants)
            db.commit() 

    new_item = models.Messages(
        conversation_id=conversation_id,
        sender_id=user.id,
        body=message.body,
        attach_doc=message.attach_doc,
        read=False,
        status='active'
    )
    db.add(new_item)
    db.commit()

    return BaseController.success([], messages[selected_language]['message_created'])

@router.post("/join-conversation/{conversation_id}", dependencies=[Depends(admin_required)])
def add_chat_message(
    conversation_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en')
):
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)
    participants = db.query(models.ConversationParticipants).filter_by(conversation_id=conversation_id, participant_id=user.id).all()
    if not participants:
        participants = [
            models.ConversationParticipants(conversation_id=conversation_id, participant_id=user.id, status='active')
        ]
        db.bulk_save_objects(participants)
        db.commit()

    return BaseController.success([], messages[selected_language]['participant_added'])

# ──────────────────────────────────────────────
# Dispute Actions & Stripe
# ──────────────────────────────────────────────
@router.post("/dispute_action/{dispute_id}", dependencies=[Depends(admin_required)])
async def dispute_action(
    dispute_id: int,
    form_data: admin.UpdateDispute,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en')
):
    try:
        token_data = verify_access_token(token)
        user = db.query(models.User).filter(models.User.email == token_data.email).first()
        if not user:
            return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)
        user_id = user.id
        
        # Fetch the booking dispute
        booking_dispute = db.query(models.BookingDispute).filter_by(id=dispute_id).first()
        if not booking_dispute:
            return BaseController.errorGeneral(
                messages[selected_language]["booking_dispute_not_found"], 
                404
            )

        # Cancel the Stripe PaymentIntent
        try:
            # Resolve the platform from the PaymentIntent's owning supplier so the
            # call hits the right Stripe account (BR vs CH).
            owning_transaction = (
                db.query(models.BookingTransaction)
                .options(joinedload(models.BookingTransaction.booking).joinedload(models.Booking.supplier))
                .filter(models.BookingTransaction.payment_intent == form_data.payment_intent)
                .first()
            )
            owning_supplier = (
                owning_transaction.booking.supplier
                if owning_transaction and owning_transaction.booking else None
            )
            api_key = stripe_api_key_for(owning_supplier)

            if form_data.dispute_action == "cancel":
                payment_stripe_response = stripe.PaymentIntent.cancel(form_data.payment_intent, api_key=api_key)
                payment_data = payment_stripe_response
                booking_transaction = db.query(models.BookingTransaction).filter(models.BookingTransaction.payment_intent==form_data.payment_intent).first()
                if booking_transaction:
                    booking_dispute = db.query(models.BookingDispute).filter_by(booking_id=booking_transaction.booking_id).first()
                    if booking_dispute:
                        # Update dispute status
                        booking_dispute.dispute_status = "1"  # Closed
                        booking_dispute.updated_at = datetime.now()
                        db.commit()
                    booking_transaction.trx_id = payment_data.get('id')
                    booking_transaction.trx_status = '4'
                    booking_transaction.trx_response = payment_data
                    booking_transaction.booking.status = 'cancelled'
                    db.commit()
            elif form_data.dispute_action == "capture":
                payment_stripe_response = stripe.PaymentIntent.capture(form_data.payment_intent, api_key=api_key)
                payment_data = payment_stripe_response
                booking_transaction = (db.query(models.BookingTransaction).options(joinedload(models.BookingTransaction.booking)).filter(models.BookingTransaction.payment_intent==form_data.payment_intent).first())
                if booking_transaction:
                    booking_dispute = db.query(models.BookingDispute).filter_by(booking_id=booking_transaction.booking_id).first()
                    if booking_dispute:
                        # Update dispute status
                        booking_dispute.dispute_status = "1"  # Closed
                        booking_dispute.updated_at = datetime.now()
                        db.commit()
                    booking_transaction.trx_id = payment_data.get('id')
                    booking_transaction.trx_status = '1'
                    booking_transaction.trx_response = payment_data
                    booking_transaction.booking.status = 'complete_closed'
                    db.commit()
            elif form_data.dispute_action in ("full_refund", "partial_refund", "release"):
                from app.routes.bookings import (
                    _refund_transaction_full,
                    _execute_release,
                )
                from decimal import Decimal

                if not owning_transaction or not owning_transaction.is_sct:
                    return BaseController.errorGeneral(
                        "This dispute action applies only to SCT orders.",
                        status.HTTP_400_BAD_REQUEST,
                    )
                booking = owning_transaction.booking

                # Log the admin decision itself before executing
                db.add(models.BookingAuditLog(
                    booking_id=booking.id,
                    actor_id=user_id,
                    event='admin_decision',
                    payload={
                        "decision": form_data.dispute_action,
                        "amount": form_data.amount,
                        "reason": form_data.reaction_reason,
                    },
                ))
                db.commit()

                release_succeeded = True
                if form_data.dispute_action == "full_refund":
                    _refund_transaction_full(
                        db, owning_transaction,
                        api_key=api_key,
                        actor_id=user_id,
                        reason=form_data.reaction_reason or 'admin_full_refund',
                    )
                    booking.status = 'cancelled'
                elif form_data.dispute_action == "partial_refund":
                    if form_data.amount is None or form_data.amount <= 0:
                        return BaseController.errorGeneral(
                            "Partial refund requires a positive amount.",
                            status.HTTP_400_BAD_REQUEST,
                        )
                    remaining = Decimal(str(owning_transaction.amount or 0)) - Decimal(str(owning_transaction.refunded_amount or 0))
                    partial = Decimal(str(form_data.amount))
                    if partial >= remaining:
                        return BaseController.errorGeneral(
                            "Partial refund amount must be less than the remaining balance. Use full_refund instead.",
                            status.HTTP_400_BAD_REQUEST,
                        )
                    stripe_fee_amount = Decimal(str(owning_transaction.stripe_fee_amount or 0))
                    max_partial_refund = remaining - stripe_fee_amount
                    if max_partial_refund < 0:
                        max_partial_refund = Decimal("0")
                    if partial > max_partial_refund:
                        return BaseController.errorGeneral(
                            (
                                "Partial refund cannot cut into the Stripe processing fee. "
                                "Use Full Refund instead, or reduce the refund to no more "
                                f"than {float(max_partial_refund)}."
                            ),
                            status.HTTP_400_BAD_REQUEST,
                        )
                    # Refund the partial amount, then release the rest.
                    partial_row = models.BookingRefund(
                        booking_transaction_id=owning_transaction.id,
                        amount=partial,
                        reason=form_data.reaction_reason or 'admin_partial_refund',
                        refunded_by=user_id,
                        status='pending',
                    )
                    db.add(partial_row)
                    db.flush()
                    try:
                        stripe_refund = stripe.Refund.create(
                            payment_intent=owning_transaction.payment_intent,
                            amount=int(partial * 100),
                            api_key=api_key,
                            metadata={
                                "meiappli_order_number": booking.order_number or "",
                                "booking_id": str(booking.id),
                                "refund_type": "partial",
                                "refund_reason": form_data.reaction_reason or "",
                            },
                        )
                        partial_row.stripe_refund_id = stripe_refund.get('id')
                        partial_row.status = 'succeeded'
                        partial_row.stripe_response = stripe_refund
                        owning_transaction.refunded_amount = (
                            Decimal(str(owning_transaction.refunded_amount or 0)) + partial
                        )
                        owning_transaction.refunded_at = datetime.now()
                        db.add(models.BookingAuditLog(
                            booking_id=booking.id,
                            actor_id=user_id,
                            event='refund_issued',
                            payload={
                                "amount": str(partial),
                                "reason": form_data.reaction_reason,
                                "stripe_refund_id": partial_row.stripe_refund_id,
                            },
                        ))
                        db.commit()
                    except Exception as exc:
                        partial_row.status = 'failed'
                        db.commit()
                        return BaseController.errorGeneral(
                            messages[selected_language].get('something_went_wrong', 'Refund failed'),
                            status.HTTP_502_BAD_GATEWAY,
                        )
                    # Release the remainder to the supplier.
                    transfer_id = _execute_release(
                        db, booking, owning_transaction,
                        api_key=api_key,
                        actor_id=user_id,
                        event_name='transfer_released',
                    )
                    release_succeeded = transfer_id is not None
                else:  # release
                    transfer_id = _execute_release(
                        db, booking, owning_transaction,
                        api_key=api_key,
                        actor_id=user_id,
                        event_name='transfer_released',
                    )
                    release_succeeded = transfer_id is not None

                if not release_succeeded:
                    # Transfer failed — keep the booking in dispute, leave the
                    # dispute row open so admin can retry once the underlying
                    # cause (missing source_transaction, connect account
                    # issue) is resolved. _execute_release already flipped
                    # transfer_status='failed' and wrote the audit row.
                    booking.status = 'dispute'
                    db.commit()
                    return BaseController.errorGeneral(
                        messages[selected_language].get('something_went_wrong', 'Transfer failed'),
                        status.HTTP_502_BAD_GATEWAY,
                    )

                # Close the dispute row only when the transfer actually went out.
                booking_dispute = db.query(models.BookingDispute).filter_by(booking_id=booking.id).first()
                if booking_dispute:
                    booking_dispute.dispute_status = "1"
                    booking_dispute.reacted_by = user_id
                    booking_dispute.reaction_reason = form_data.reaction_reason
                    booking_dispute.updated_at = datetime.now()
                db.commit()
            else:
                return BaseController.errorGeneral(
                    messages[selected_language]["something_wrong"], 
                    404
                )
                
            # Update dispute status
            booking_dispute.dispute_status = "1"  # Closed
            booking_dispute.reacted_by = user_id
            booking_dispute.reaction_reason = form_data.reaction_reason
            booking_dispute.updated_at = datetime.now()
            db.commit()

            booking = (db.query(models.Booking)
               .filter(models.Booking.id == booking_dispute.booking_id)
               .options(joinedload(models.Booking.supplier).joinedload(models.Supplier.user))
               .options(joinedload(models.Booking.user)).first())
            if booking:
                customer_template_content = get_email_template_content(db, booking.user1.language_id, 'order-updated')
                supplier_template_content = get_email_template_content(db, booking.supplier.user.language_id, 'order-updated')
                customer_payment_status = messages[booking.user1.language.code]["offline_booking"]
                supplier_payment_status = messages[booking.supplier.user.language.code]["offline_booking"]
                if booking.payment_mode == "online":
                    customer_payment_status = messages[booking.user1.language.code]["online_booking"]
                    supplier_payment_status = messages[booking.supplier.user.language.code]["online_booking"]
                if customer_template_content and booking.user1.send_mail_notification == "0":
                    background_tasks.add_task(
                        send_order_email,
                        email=booking.user1.email,
                        order_number=booking.order_number,
                        subject=customer_template_content.subject,
                        body=customer_template_content.body,
                        first_name=decrypt_data(booking.user1.first_name),
                        last_name=decrypt_data(booking.user1.last_name),
                        status=messages[booking.user1.language.code][booking.status],
                        booking_id=booking.order_number,
                        customer_name=booking.customer_name,
                        service_provider_name=booking.supplier_name,
                        price=f"{booking.supplier.user.currency.symbol}{str(booking.total_price)}",
                        booking_date=(booking.order_date_time).strftime("%d/%m/%Y"),
                        booking_time=(booking.order_date_time).strftime("%H:%M"),
                        payment_mode=customer_payment_status,
                        selected_language=booking.user1.language.code
                    )
                if supplier_template_content and booking.supplier.user.send_mail_notification == "0":
                    background_tasks.add_task(
                        send_order_email,
                        email=booking.supplier.user.email,
                        order_number=booking.order_number,
                        subject=supplier_template_content.subject,
                        body=supplier_template_content.body,
                        first_name=decrypt_data(booking.supplier.user.first_name),
                        last_name=decrypt_data(booking.supplier.user.last_name),
                        status=messages[booking.supplier.user.language.code][booking.status],
                        booking_id=booking.order_number,
                        customer_name=booking.customer_name,
                        service_provider_name=booking.supplier_name,
                        price=f"{booking.supplier.user.currency.symbol}{str(booking.total_price)}",
                        booking_date=(booking.order_date_time).strftime("%d/%m/%Y"),
                        booking_time=(booking.order_date_time).strftime("%H:%M"),
                        payment_mode=supplier_payment_status,
                        selected_language=booking.supplier.user.language.code
                    )

            # Return success response
            return BaseController.success(
                [], 
                messages[selected_language]["booking_dispute_updated"]
            )
        except stripe.error.StripeError as e:
            return BaseController.errorGeneral(
                f"Stripe Error: {e.user_message if hasattr(e, 'user_message') else str(e)}", 
                400
            )

    except HTTPException as e:
        return BaseController.errorGeneral(
            f"Internal Server Error: {str(e)}", 
            500
        )
    except Exception as e:
        # General error handling
        return BaseController.errorGeneral(
            f"Internal Server Error: {str(e)}", 
            500
        )

@router.put("/update_profile", summary="Update Admin Profile", dependencies=[Depends(admin_required)])
async def update_admin_profile(
    update_data: admin.UpdateAdminProfile,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en')
):
    try:
        token_data = verify_access_token(token)
        user = db.query(models.User).join(models.Role).filter(models.User.email == token_data.email, models.Role.guard_name == 'admin').first()
        if not user:
            return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)
        
        if update_data.first_name:
            user.first_name = update_data.first_name
        if update_data.last_name:
            user.last_name = update_data.last_name
        if update_data.password:
            user.password = hash_password(update_data.password)

        # Commit the changes
        db.add(user)
        db.commit()
        db.refresh(user)
        return BaseController.success([], messages[selected_language]["user_detail_updated"])
    except Exception as e:
        # General error handling
        return BaseController.errorGeneral(
            f"Internal Server Error: {str(e)}", 
            500
        )

@router.get("/user_analytics", dependencies=[Depends(admin_required)])
async def get_user_analytics(
    filters: admin.UserAnalytics = Depends(),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    try:
        # Base query for users with role_id == '2' (assumed to be regular users)
        users_query = db.query(models.User.status, func.count(models.User.id).label("count")).filter(models.User.role_id == 2)

        # Apply date filters
        if filters.created_from_date or filters.created_to_date:
            date_filters = []
            if filters.created_from_date:
                date_filters.append(
                    cast(models.User.created_at, Date) >= convert_date(filters.created_from_date)
                )
            if filters.created_to_date:
                date_filters.append(
                    cast(models.User.created_at, Date) <= convert_date(filters.created_to_date)
                )
            users_query = users_query.filter(and_(*date_filters))

        # Apply status filter
        if filters.user_status and filters.user_status.lower() != "all":
            statuses = [status.strip().lower() for status in filters.user_status.split(",")]
            users_query = users_query.filter(models.User.status.in_(statuses))

        # Apply device type filter
        if filters.device_type and filters.device_type.lower() != "all":
            device_types = [hash_string(device_type) for device_type in filters.device_type.split(",")]
            users_query = users_query.filter(models.User.device_type_hash.in_(device_types))

        # Apply country filter
        if filters.country:
            country_ids = [int(id.strip()) for id in filters.country.split(",")]
            users_query = users_query.filter(models.User.country_id.in_(country_ids))

        # Group by status and fetch results
        user_data = users_query.group_by(models.User.status).all()

        # Format the result as key-value pairs
        result = {status: count for status, count in user_data}

        user_chart_data = {
            "total": sum(result.values()),
            "status_counts": result
        }
        return BaseController.success(jsonable_encoder(user_chart_data), messages[selected_language]["user_analytics_list"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching data: {str(e)}")

@router.get("/booking_analytics", dependencies=[Depends(admin_required)])
async def get_booking_analytics(
    filters: admin.BookingAnalytics = Depends(),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    try:
        # Initialize base query
        booking_query = db.query(models.Booking.status, func.count(models.Booking.id).label("count"))

        # Apply date filters
        if filters.booking_from_date or filters.booking_to_date:
            date_filters = []
            if filters.booking_from_date:
                date_filters.append(
                    cast(models.Booking.booking_date_time, Date) >= convert_date(filters.booking_from_date)
                )
            if filters.booking_to_date:
                date_filters.append(
                    cast(models.Booking.booking_date_time, Date) <= convert_date(filters.booking_to_date)
                )
            booking_query = booking_query.filter(and_(*date_filters))

        # Apply status filter
        if filters.booking_status and filters.booking_status.lower() != "all":
            statuses = [status.strip().lower() for status in filters.booking_status.split(",")]
            booking_query = booking_query.filter(models.Booking.status.in_(statuses))

        # Group by status and fetch results
        booking_data = booking_query.group_by(models.Booking.status).all()

        # Format data into key-value pairs
        result = {" ".join(word.capitalize() for word in status.split("_")): count for status, count in booking_data}

        booking_chart_data = {
            "total": sum(result.values()),
            "status_counts": result
        }
        return BaseController.success(jsonable_encoder(booking_chart_data), messages[selected_language]["user_bookings"])
    except Exception as e:
        return BaseController.errordefault_lang_org_nameGeneral(messages[selected_language]['something_wrong'], status.HTTP_404_NOT_FOUND)

@router.get("/transaction_analytics", summary="Get transaction analytics", dependencies=[Depends(admin_required)])
async def get_transaction_analytics(
    filters: admin.TransactionAnalytics = Depends(),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    # Base query for transactions
    transactions_query = db.query(
        models.BookingTransaction.trx_status,
        func.count(models.BookingTransaction.id).label("count"),
    )

    # Apply filters
    if filters.created_from_date or filters.created_to_date:
        date_filters = []
        if filters.created_from_date:
            date_filters.append(
                cast(models.BookingTransaction.created_at, Date) >= convert_date(filters.created_from_date)
            )
        if filters.created_to_date:
            date_filters.append(
                cast(models.BookingTransaction.created_at, Date) <= convert_date(filters.created_to_date)
            )
        transactions_query = transactions_query.filter(and_(*date_filters))
    
    if filters.trx_status and filters.trx_status.lower() != "all":
        statuses = [status.strip() for status in filters.trx_status.split(",")]
        transactions_query = transactions_query.filter(models.BookingTransaction.trx_status.in_(statuses))
    
    if filters.amount_min is not None:
        transactions_query = transactions_query.filter(models.BookingTransaction.amount >= filters.amount_min)
    
    if filters.amount_max is not None:
        transactions_query = transactions_query.filter(models.BookingTransaction.amount <= filters.amount_max)

    # Group by transaction status
    transaction_data = transactions_query.group_by(models.BookingTransaction.trx_status).all()

    # Format results
    result = {}
    booking_mapping = {"0": "Failed", "1": "Success", "2": "Pending", "3": "Expired", "4": "Cancelled"}
    for trx_status, count in transaction_data:
        result[booking_mapping[trx_status]] = count
    
    booking_chart_data = {
        "total": sum(item for item in result.values()),
        "status_counts": result
    }
    return BaseController.success(jsonable_encoder(booking_chart_data), messages[selected_language]["user_bookings"])


def sort_with_others_last(items, key_name):
    """Sort a list of dictionaries by a key, placing 'Others' at the end."""
    return sorted(items, key=lambda x: (x[key_name] == "Others", x[key_name]))

# ──────────────────────────────────────────────
# Transactions
# ──────────────────────────────────────────────
@router.get("/get-transactions", dependencies=[Depends(admin_required)])
def get_transactions(
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    results = (
        db.query(
            models.Country.id.label("country_id"),
            models.Country.country_name.label("country_name"),
            models.Booking.district,
            models.Booking.state,
            models.Booking.city,
            models.Booking.booking_address,
            models.Booking.payment_mode,
            models.User.gender,
            func.sum(models.Booking.total_price).label("total_earnings"),
        )
        .outerjoin(models.Country, models.Country.id == models.Booking.country_id)
        .join(models.Supplier, models.Supplier.id == models.Booking.supplier_id)
        .join(models.User, models.User.id == models.Supplier.user_id)
        .filter(models.Supplier.status == "Approved")
        .filter(models.User.status == "Approved")
        .group_by(
            models.Country.id,
            models.Country.country_name,
            models.Booking.district,
            models.Booking.state,
            models.Booking.city,
            models.Booking.booking_address,
            models.Booking.payment_mode,
            models.User.gender
        )
        .all()
    )

    payment_mode_data = {
        "online": defaultdict(
            lambda: {
                "country_id": None,
                "country_name": None,
                "total_country_earnings": 0,
                "districts": defaultdict(
                    lambda: {
                        "district_name": None,
                        "total_district_earnings": 0,
                        "cities": defaultdict(
                            lambda: {
                                "city_name": None,
                                "total_city_earning": 0,
                                "male_supplier_earning": 0,
                                "female_supplier_earning": 0,
                                "others": 0,
                            }
                        ),
                    }
                ),
                "cities": defaultdict(
                    lambda: {
                        "city_name": None,
                        "total_city_earning": 0,
                        "male_supplier_earning": 0,
                        "female_supplier_earning": 0,
                        "others": 0,
                    }
                ),
            }
        ),
        "offline": defaultdict(
            lambda: {
                "country_id": None,
                "country_name": None,
                "total_country_earnings": 0,
                "districts": defaultdict(
                    lambda: {
                        "district_name": None,
                        "total_district_earnings": 0,
                        "cities": defaultdict(
                            lambda: {
                                "city_name": None,
                                "total_city_earning": 0,
                                "male_supplier_earning": 0,
                                "female_supplier_earning": 0,
                                "others": 0,
                            }
                        ),
                    }
                ),
                "cities": defaultdict(
                    lambda: {
                        "city_name": None,
                        "total_city_earning": 0,
                        "male_supplier_earning": 0,
                        "female_supplier_earning": 0,
                        "others": 0,
                    }
                ),
            }
        ),
    }

    for country_id, country_name, district, state, city, booking_address, payment_mode, gender, total_earnings in results:
        # payment_mode_data only has 'online' / 'offline' buckets at the top level.
        # Skip rows whose payment_mode is NULL / empty / a typo — they have nowhere
        # to slot in the response shape and would crash with KeyError.
        if payment_mode not in payment_mode_data:
            continue
        if not country_id:
            if booking_address:
                country_name = booking_address.split(",")[-1].strip()
            else:
                country_name = "Others"
            district = None
            state = None

        country = payment_mode_data[payment_mode][country_name]
        country["country_name"] = country_name
        country["total_country_earnings"] += round(total_earnings or 0, 2)

        if country_id:
            country["country_id"] = country_id
            district_name = district or state or "Others"
            district_data = country["districts"][district_name]
            district_data["district_name"] = district_name
            district_data["total_district_earnings"] += round(total_earnings or 0, 2)

            city_data = district_data["cities"][city]
        else:
            city_data = country["cities"][city]

        city_data["city_name"] = city if city else "Others"
        city_data["total_city_earning"] += round(total_earnings or 0, 2)

        if gender == "0":
            city_data["male_supplier_earning"] += round(total_earnings or 0, 2)
        elif gender == "1":
            city_data["female_supplier_earning"] += round(total_earnings or 0, 2)
        elif gender == "2":
            city_data["others"] += round(total_earnings or 0, 2)

    output = {
        "online": [],
        "offline": []
    }

    for payment_mode, data in payment_mode_data.items():
        # Bookings rows whose payment_mode is NULL / empty / anything other than
        # 'online' or 'offline' have nowhere to slot in the response shape. Skip
        # them rather than 500ing the whole endpoint; the source of the bad value
        # should be cleaned at the booking-create layer separately.
        if payment_mode not in output:
            continue
        for country_name, country_info in data.items():
            if country_info["districts"]:
                district_list = []
                for district_name, district_info in country_info["districts"].items():
                    district_info["cities"] = sort_with_others_last(list(district_info["cities"].values()), "city_name")
                    district_list.append(district_info)
                country_info["districts"] = sort_with_others_last(district_list, "district_name")
                country_info["cities"] = None
            else:
                country_info["districts"] = None
                country_info["cities"] = sort_with_others_last(list(country_info["cities"].values()), "city_name")
            output[payment_mode].append(country_info)

    # Sort countries at the top level
    for mode in output:
        output[mode] = sort_with_others_last(output[mode], "country_name")

    return BaseController.success(jsonable_encoder(output), messages[selected_language]["user_analytics_list"])

def _parse_filter_date(value: str):
    for fmt in ("%Y-%m-%d", "%d %b %y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _resolve_tz(user_timezone: Optional[str]):
    if not user_timezone:
        return None
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            return ZoneInfo(user_timezone)
        except ZoneInfoNotFoundError:
            return None
    except Exception:
        return None


def _to_user_tz(dt, tz):
    if dt is None:
        return None
    if tz is None:
        return dt
    from datetime import timezone as _tz
    aware = dt if dt.tzinfo else dt.replace(tzinfo=_tz.utc)
    return aware.astimezone(tz).replace(tzinfo=None)


def _apply_booking_transaction_filters(
    query, *, transfer_status, gender, country_id, region, city, search, from_date, to_date,
    user_timezone: Optional[str] = None,
):
    if transfer_status:
        query = query.filter(models.BookingTransaction.transfer_status == transfer_status)
    if gender:
        query = query.filter(models.User.gender == gender)
    if country_id:
        query = query.filter(models.Booking.country_id == country_id)
    if region:
        query = query.filter(models.Booking.state == region)
    if city:
        query = query.filter(models.Booking.city == city)
    if search:
        query = query.filter(models.Booking.order_number.ilike(f"%{search.strip()}%"))
    tz = _resolve_tz(user_timezone)
    from datetime import timedelta as _td, timezone as _tz
    if from_date:
        parsed = _parse_filter_date(from_date)
        if parsed is not None:
            if tz is not None:
                start_local = datetime.combine(parsed, datetime.min.time()).replace(tzinfo=tz)
                start_utc = start_local.astimezone(_tz.utc).replace(tzinfo=None)
                query = query.filter(models.BookingTransaction.created_at >= start_utc)
            else:
                query = query.filter(cast(models.BookingTransaction.created_at, Date) >= parsed)
    if to_date:
        parsed = _parse_filter_date(to_date)
        if parsed is not None:
            if tz is not None:
                end_local = datetime.combine(parsed + _td(days=1), datetime.min.time()).replace(tzinfo=tz)
                end_utc = end_local.astimezone(_tz.utc).replace(tzinfo=None)
                query = query.filter(models.BookingTransaction.created_at < end_utc)
            else:
                query = query.filter(cast(models.BookingTransaction.created_at, Date) <= parsed)
    return query


def _fmt_money(value) -> Optional[str]:
    if value is None:
        return None
    return f"{Decimal(str(value)):.2f}"


@router.get("/get-booking-transactions", dependencies=[Depends(admin_required)])
def get_booking_transactions(
    transfer_status: Optional[str] = Query(default=None),
    gender: Optional[str] = Query(default=None),
    country_id: Optional[int] = Query(default=None, ge=0),
    region: Optional[str] = Query(default=None),
    city: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None, description="Search by order number"),
    from_date: Optional[str] = Query(default=None, description="Filter by created_at from date (dd/mm/yy)"),
    to_date: Optional[str] = Query(default=None, description="Filter by created_at to date (dd/mm/yy)"),
    user_timezone: Optional[str] = Query(default=None, description="IANA timezone e.g. America/Sao_Paulo"),
    sort_by = Query(default='created_at'),
    sort_order = Query(default='desc'),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
    page: int = Query(default=0, ge=0, description="Page number for pagination"),
    limit: int = Query(default=10, ge=1, le=100, description="Number of bookings to return per page")
):
    transactions_query = (db.query(models.BookingTransaction)
                          .join(models.Booking, models.Booking.id == models.BookingTransaction.booking_id)
                          .join(models.Supplier, models.Supplier.id == models.Booking.supplier_id)
                          .join(models.User, models.User.id == models.Supplier.user_id)
                          .filter(
                              models.BookingTransaction.trx_id.isnot(None),
                              models.BookingTransaction.trx_id != '',
                          )
    )
    transactions_query = _apply_booking_transaction_filters(
        transactions_query,
        transfer_status=transfer_status,
        gender=gender,
        country_id=country_id,
        region=region,
        city=city,
        search=search,
        from_date=from_date,
        to_date=to_date,
        user_timezone=user_timezone,
    )

    total_bookings = transactions_query.count()
    sort_columns = {
        "order_number": models.Booking.order_number,
        "amount": models.BookingTransaction.amount,
        "created_at": models.BookingTransaction.created_at,
        "updated_at": models.BookingTransaction.updated_at,
    }
    if sort_by in sort_columns:
        sort_column = sort_columns[sort_by]
        if sort_order == 'asc':
            transactions_query = transactions_query.order_by(asc(sort_column))
        else:
            transactions_query = transactions_query.order_by(desc(sort_column))
    transactions = transactions_query.offset(page * limit).limit(limit).all()
    response_data = []
    for transaction in transactions:
        booking = transaction.booking
        response_data.append(admin.GetAdminTransactions(
            id=transaction.id,
            booking_id=transaction.booking_id,
            order_number=booking.order_number if booking else "",
            gross_amount=_fmt_money(transaction.amount),
            stripe_fee_amount=_fmt_money(transaction.stripe_fee_amount),
            participation_fee=_fmt_money(transaction.participation_fee),
            refunded_amount=_fmt_money(transaction.refunded_amount),
            net_amount=_fmt_money(transaction.net_amount),
            payment_holding_days=transaction.payment_holding_days,
            stripe_charge_id=transaction.stripe_charge_id,
            transfer_id=transaction.transfer_id,
            transfer_status=transaction.transfer_status,
            currency_code=booking.currency_code if booking else None,
            currency_symbol=booking.currency_symbol if booking else None,
            is_sct=bool(transaction.is_sct),
            created_at=transaction.created_at,
            updated_at=transaction.updated_at,
        ))
    countries = (
        db.query(models.Country.id, models.Country.country_name)
        .join(models.Booking, models.Booking.country_id == models.Country.id)
        .distinct()
        .all()
    )
    regions = []
    cities = []
    if country_id:
        regions = (
            db.query(models.Booking.state)
            .filter(
                models.Booking.country_id == country_id,
                models.Booking.state.isnot(None),
                models.Booking.state != "",
            )
            .distinct()
            .all()
        )
        cities = (
            db.query(models.Booking.city)
            .filter(
                models.Booking.country_id == country_id,
                models.Booking.city.isnot(None),
                models.Booking.city != "",
            )
            .distinct()
            .all()
        )
    regions = [region.state for region in regions]
    cities = [city.city for city in cities]
    return PaginationResponse(
        total=total_bookings,
        page=page,
        per_page=limit,
        data={
            "transactions": response_data,
            "countries": [{"id": country.id, "name": country.country_name} for country in countries],
            "regions": regions,
            "cities": cities,
        },
        message=messages[selected_language]['user_bookings']
    )


@router.get("/get-booking-transactions/summary", dependencies=[Depends(admin_required)])
def get_booking_transactions_summary(
    transfer_status: Optional[str] = Query(default=None),
    gender: Optional[str] = Query(default=None),
    country_id: Optional[int] = Query(default=None, ge=0),
    region: Optional[str] = Query(default=None),
    city: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    from_date: Optional[str] = Query(default=None),
    to_date: Optional[str] = Query(default=None),
    user_timezone: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
):
    bt = models.BookingTransaction
    is_sct = bt.is_sct.is_(True)
    is_success = bt.trx_status == '1'
    is_cancelled = bt.trx_status == '4'
    gross_expr = case(
        (is_sct, bt.amount),
        (bt.trx_status.in_(['1', '2', '4']), bt.amount),
        else_=0,
    )
    refunded_expr = case(
        (is_sct, func.coalesce(bt.refunded_amount, 0)),
        (and_(~is_sct, is_cancelled), bt.amount),
        else_=0,
    )
    fees_expr = case(
        (is_sct, func.coalesce(bt.stripe_fee_amount, 0)),
        else_=0,
    )
    participation_expr = case(
        (is_sct, func.coalesce(bt.participation_fee, 0)),
        else_=0,
    )
    net_expr = case(
        (is_sct, func.coalesce(bt.net_amount, 0)),
        (and_(~is_sct, is_success), bt.amount),
        else_=0,
    )
    query = (
        db.query(
            models.Booking.currency_code.label('currency_code'),
            models.Booking.currency_symbol.label('currency_symbol'),
            func.coalesce(func.sum(gross_expr), 0).label('gross_payments'),
            func.coalesce(func.sum(fees_expr), 0).label('stripe_fees'),
            func.coalesce(func.sum(participation_expr), 0).label('participation_fees'),
            func.coalesce(func.sum(refunded_expr), 0).label('refunded_amount'),
            func.coalesce(func.sum(net_expr), 0).label('net_to_suppliers'),
            func.count(bt.id).label('transactions_count'),
        )
        .join(models.Booking, models.Booking.id == bt.booking_id)
        .join(models.Supplier, models.Supplier.id == models.Booking.supplier_id)
        .join(models.User, models.User.id == models.Supplier.user_id)
        .filter(bt.trx_id.isnot(None), bt.trx_id != '')
    )
    query = _apply_booking_transaction_filters(
        query,
        transfer_status=transfer_status,
        gender=gender,
        country_id=country_id,
        region=region,
        city=city,
        search=search,
        from_date=from_date,
        to_date=to_date,
        user_timezone=user_timezone,
    )
    rows = query.group_by(models.Booking.currency_code, models.Booking.currency_symbol).all()
    by_currency = [
        {
            "currency_code": row.currency_code,
            "currency_symbol": row.currency_symbol,
            "transactions_count": int(row.transactions_count),
            "gross_payments": _fmt_money(row.gross_payments),
            "stripe_fees": _fmt_money(row.stripe_fees),
            "participation_fees": _fmt_money(row.participation_fees),
            "refunded_amount": _fmt_money(row.refunded_amount),
            "net_to_suppliers": _fmt_money(row.net_to_suppliers),
        }
        for row in rows
    ]
    return BaseController.success(
        jsonable_encoder({"by_currency": by_currency}),
        messages[selected_language]['user_bookings'],
    )


@router.get("/get-booking-transactions/export", dependencies=[Depends(admin_required)])
def export_booking_transactions(
    transfer_status: Optional[str] = Query(default=None),
    gender: Optional[str] = Query(default=None),
    country_id: Optional[int] = Query(default=None, ge=0),
    region: Optional[str] = Query(default=None),
    city: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    from_date: Optional[str] = Query(default=None),
    to_date: Optional[str] = Query(default=None),
    user_timezone: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
):
    query = (
        db.query(models.BookingTransaction)
        .join(models.Booking, models.Booking.id == models.BookingTransaction.booking_id)
        .join(models.Supplier, models.Supplier.id == models.Booking.supplier_id)
        .join(models.User, models.User.id == models.Supplier.user_id)
        .filter(
            models.BookingTransaction.trx_id.isnot(None),
            models.BookingTransaction.trx_id != '',
        )
    )
    query = _apply_booking_transaction_filters(
        query,
        transfer_status=transfer_status,
        gender=gender,
        country_id=country_id,
        region=region,
        city=city,
        search=search,
        from_date=from_date,
        to_date=to_date,
        user_timezone=user_timezone,
    )
    query = query.order_by(desc(models.BookingTransaction.created_at))
    all_txns = query.all()

    bt = models.BookingTransaction
    is_sct = bt.is_sct.is_(True)
    is_success = bt.trx_status == '1'
    is_cancelled = bt.trx_status == '4'
    gross_expr = case(
        (is_sct, bt.amount),
        (bt.trx_status.in_(['1', '2', '4']), bt.amount),
        else_=0,
    )
    refunded_expr = case(
        (is_sct, func.coalesce(bt.refunded_amount, 0)),
        (and_(~is_sct, is_cancelled), bt.amount),
        else_=0,
    )
    fees_expr = case(
        (is_sct, func.coalesce(bt.stripe_fee_amount, 0)),
        else_=0,
    )
    participation_expr = case(
        (is_sct, func.coalesce(bt.participation_fee, 0)),
        else_=0,
    )
    net_expr = case(
        (is_sct, func.coalesce(bt.net_amount, 0)),
        (and_(~is_sct, is_success), bt.amount),
        else_=0,
    )
    summary_query = (
        db.query(
            models.Booking.currency_code.label('currency_code'),
            func.coalesce(func.sum(gross_expr), 0).label('gross_payments'),
            func.coalesce(func.sum(fees_expr), 0).label('stripe_fees'),
            func.coalesce(func.sum(participation_expr), 0).label('participation_fees'),
            func.coalesce(func.sum(refunded_expr), 0).label('refunded_amount'),
            func.coalesce(func.sum(net_expr), 0).label('net_to_suppliers'),
            func.count(bt.id).label('transactions_count'),
        )
        .join(models.Booking, models.Booking.id == bt.booking_id)
        .join(models.Supplier, models.Supplier.id == models.Booking.supplier_id)
        .join(models.User, models.User.id == models.Supplier.user_id)
        .filter(bt.trx_id.isnot(None), bt.trx_id != '')
    )
    summary_query = _apply_booking_transaction_filters(
        summary_query,
        transfer_status=transfer_status,
        gender=gender,
        country_id=country_id,
        region=region,
        city=city,
        search=search,
        from_date=from_date,
        to_date=to_date,
        user_timezone=user_timezone,
    )
    summary_rows = summary_query.group_by(models.Booking.currency_code).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Summary"])
    writer.writerow([
        "Currency", "Transactions", "Gross Payments", "Stripe Fees",
        "MEIAPPLI Participation Fees", "Refunded Amount", "Net to Suppliers",
    ])
    for row in summary_rows:
        writer.writerow([
            row.currency_code or "",
            int(row.transactions_count),
            _fmt_money(row.gross_payments) or "0.00",
            _fmt_money(row.stripe_fees) or "0.00",
            _fmt_money(row.participation_fees) or "0.00",
            _fmt_money(row.refunded_amount) or "0.00",
            _fmt_money(row.net_to_suppliers) or "0.00",
        ])
    writer.writerow([])
    writer.writerow(["Transactions"])

    tz = _resolve_tz(user_timezone)

    def _fmt_dt(dt):
        local = _to_user_tz(dt, tz)
        return local.strftime("%Y-%m-%d %H:%M:%S") if local else ""

    rows = []
    for txn in all_txns:
        booking = txn.booking
        rows.append({
            "Order Number": booking.order_number if booking else "",
            "Booking ID": txn.booking_id,
            "Currency": booking.currency_code if booking else "",
            "Gross Amount": _fmt_money(txn.amount),
            "Stripe Fee": _fmt_money(txn.stripe_fee_amount),
            "MEIAPPLI Participation Fee": _fmt_money(txn.participation_fee),
            "Refunded Amount": _fmt_money(txn.refunded_amount),
            "Net to Supplier": _fmt_money(txn.net_amount),
            "Holding Days": txn.payment_holding_days,
            "Transaction ID": txn.stripe_charge_id,
            "Transfer ID": txn.transfer_id,
            "Transfer Status": txn.transfer_status,
            "Created At": _fmt_dt(txn.created_at),
            "Updated At": _fmt_dt(txn.updated_at),
        })
    df = pd.DataFrame(rows)
    df.to_csv(buffer, index=False)
    buffer.seek(0)
    filename = f"booking_transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        buffer,
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


@router.get("/get-booking/{booking_id}", dependencies=[Depends(admin_required)])
def get_single_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    booking = db.query(models.Booking).filter(
            models.Booking.id == booking_id
        ).options(
            joinedload(models.Booking.supplier).joinedload(models.Supplier.user).joinedload(models.User.currency)
        ).first()

    if not booking:
        return BaseController.errorGeneral(messages[selected_language]['booking_not_found'], status.HTTP_404_NOT_FOUND)
    if booking.supplier:
        average_rating = db.query(func.avg(models.t_booking_reviews.c.rating_value)).filter(
            models.t_booking_reviews.c.receiver_id == booking.supplier.user_id, models.t_booking_reviews.c.reviewer_type == "1"
        ).scalar()
    else:
        average_rating = 0

    booking_items = db.query(models.BookingItem).filter(models.BookingItem.booking_id == booking.id).all()
    booking_item_ids = [bi.id for bi in booking_items]
    images_by_booking_item = {}
    if booking_item_ids:
        all_images = (
            db.query(models.BookingItemImage)
            .filter(models.BookingItemImage.booking_item_id.in_(booking_item_ids))
            .order_by(models.BookingItemImage.sort_order.asc(), models.BookingItemImage.id.asc())
            .all()
        )
        for img in all_images:
            images_by_booking_item.setdefault(img.booking_item_id, []).append(img)
    discounted_price = booking.discount if booking.discount else 0.0
    total_amount = round((booking.total_price + booking.additional_fee - booking.discount), 2)
    transaction_date = None
    booking_transaction = db.query(models.BookingTransaction).filter(models.BookingTransaction.booking_id==booking_id).first()
    if booking_transaction:
        transaction_date = booking_transaction.created_at.strftime("%d/%m/%Y")
    is_customer_supplier = bool(
        booking.user_id
        and db.query(models.Supplier.id).filter_by(user_id=booking.user_id).first()
    )
    booking_response = admin.GetAdminBookingDetail(
        id=booking.id,
        order_number=booking.order_number,
        user_id=booking.user_id,
        supplier_id=booking.supplier_id,
        booking_date_time=booking.booking_date_time,
        customer_name=booking.customer_name,
        customer_company_name=decrypt_data(booking.user1.company_name) if booking.user1 else None,
        booking_address=booking.booking_address,
        booking_address_additional_direction=booking.booking_address_additional_direction,
        supplier_name=booking.supplier_name,
        supplier_company_name=decrypt_data(booking.supplier.user.company_name) if booking.supplier and booking.supplier.user else None,
        additional_fee=f"{booking.additional_fee if booking.additional_fee else 0.0:.2f}",
        discount=f"{booking.discount if booking.discount else 0.0:.2f}",
        total_price=f"{total_amount:.2f}",
        additional_fee_reason=booking.additional_fee_reason,
        booking_additional_information=booking.booking_additional_information,
        customer_email=booking.customer_email,
        order_date_time=booking.order_date_time,
        payment_mode=booking.payment_mode,
        status=booking.status,
        cancelled_reason=booking.cancelled_reason,
        cancelled_by=booking.cancelled_by,
        rating=round(average_rating, 2) if average_rating else 0.0,
        latitude=booking.latitude,
        longitude=booking.longitude,
        city=booking.city,
        apartment_zone=booking.apartment_zone,
        supplier_email=booking.supplier.user.email if booking.supplier else None,
        supplier_user_id=booking.supplier.user_id if booking.supplier else None,
        discounted_price=f"{discounted_price:.2f}",
        currency_code = booking.currency_code,
        currency_symbol = booking.currency_symbol,
        transaction_date=transaction_date,
        bank_details_verified=booking.supplier.stripe_verification_status if booking.supplier else 'unverified',
        expense_classification=booking.expense_classification or "2",
        is_customer_supplier=is_customer_supplier,
        order_type=_derive_admin_order_type(booking_items),
        is_sct=bool(booking_transaction.is_sct) if booking_transaction else False,
        transfer_status=booking_transaction.transfer_status if booking_transaction else "pending",
        participation_fee=(f"{booking_transaction.participation_fee:.2f}"
                           if booking_transaction and booking_transaction.participation_fee is not None else None),
        stripe_fee_amount=(f"{booking_transaction.stripe_fee_amount:.2f}"
                           if booking_transaction and booking_transaction.stripe_fee_amount is not None else None),
        net_amount=(f"{booking_transaction.net_amount:.2f}"
                    if booking_transaction and booking_transaction.net_amount is not None else None),
        refunded_amount=(f"{booking_transaction.refunded_amount:.2f}"
                         if booking_transaction and booking_transaction.refunded_amount is not None else None),
        stripe_charge_id=booking_transaction.stripe_charge_id if booking_transaction else None,
        transfer_id=booking_transaction.transfer_id if booking_transaction else None,
        review_window_ends_at=(booking_transaction.review_window_ends_at if booking_transaction else None),
        released_at=(booking_transaction.released_at if booking_transaction else None),
        refunded_at=(booking_transaction.refunded_at if booking_transaction else None),
        agreement_notes=booking.agreement_notes,
        booking_items=[
            admin.GetAdminBookingItems(
                id=item.id,
                supplier_item_id=item.supplier_item_id,
                item_name=item.item_name,
                item_price=f"{item.item_price:.2f}",
                item_unit=item.item_unit,
                item_type=item.item_type,
                service_started=bool(item.service_started),
                service_started_at=item.service_started_at,
                service_ended=bool(item.service_ended),
                service_ended_at=item.service_ended_at,
                quantity=item.quantity,
                hours=item.hours,
                minutes=item.minutes,
                currency_code = booking.currency_code,
                currency_symbol = booking.currency_symbol,
                item_image = item.item_image,
                item_images=[
                    booking_model.BookingItemImage(
                        id=img.id,
                        image_url=img.image_url,
                        sort_order=img.sort_order,
                    )
                    for img in images_by_booking_item.get(item.id, [])
                ],
            ) for item in booking_items
        ]
    )

    return BaseController.success(jsonable_encoder(booking_response), messages[selected_language]['user_booking'])

@router.delete("/nationalities/{nationality_id}", dependencies=[Depends(admin_required)])
def delete_nationality(nationality_id: int, db: Session = Depends(get_db)):
    db_nationality = db.query(models.Nationality).filter(models.Nationality.id == nationality_id).first()
    if not db_nationality:
        return BaseController.errorGeneral("Nationality not found", 404)
    
    db.query(models.NationalityTranslation).filter(models.NationalityTranslation.nationality_id == nationality_id).delete()
    db.delete(db_nationality)
    db.commit()
    
    return BaseController.success([], "Nationality deleted successfully")

@router.delete("/countries/{country_id}", dependencies=[Depends(admin_required)])
def delete_country(country_id: int, db: Session = Depends(get_db)):
    db_country = db.query(models.Country).filter(models.Country.id == country_id).first()
    if not db_country:
        return BaseController.errorGeneral("Country not found", 404)
    
    db.query(models.CountryTranslation).filter(models.CountryTranslation.country_id == country_id).delete()
    db.delete(db_country)
    db.commit()
    
    return BaseController.success([], "Country deleted successfully")


# ──────────────────────────────────────────────
# Excel Upload Helpers
# ──────────────────────────────────────────────
def _parse_excel_file(contents, filename):
    temp_path = f"temp_{filename}"
    with open(temp_path, "wb") as f:
        f.write(contents)
    try:
        df = pd.read_excel(temp_path)
    finally:
        os.remove(temp_path)
    return df


def _create_org_subscription(db: Session, supplier_id, duration, subscription_type=SubscriptionType.STANDARD):
    expiry_date = datetime.now() + timedelta(days=int(duration))
    new_subscription = models.SupplierSubscription(
        supplier_id=supplier_id,
        order_id='',
        product_id='',
        purchase_time='',
        purchase_state=1,
        purchase_token='',
        quantity=1,
        status='active',
        subscription_type=subscription_type,
        expiry_date=expiry_date,
        personal_subscription=False,
        personal_subscription_pending_days=0,
    )
    db.add(new_subscription)


def _process_assigned_user(db: Session, assigned_user, organization, duration, subscription_type=SubscriptionType.STANDARD):
    days_left = 0
    notification_message = "approved_user"
    email_message = "approved-user"

    already_added_supplier = db.query(models.Supplier).filter(models.Supplier.user_id == assigned_user.id).first()
    if not already_added_supplier:
        return assigned_user.id, days_left, notification_message, email_message

    existing_schedule_entries = db.query(models.SupplierItemSchedule).filter(
        models.SupplierItemSchedule.supplier_id == already_added_supplier.id
    ).first()
    if not existing_schedule_entries:
        return assigned_user.id, days_left, notification_message, email_message

    grant_days = int(duration or 0)
    if grant_days <= 0:
        return assigned_user.id, days_left, notification_message, email_message

    has_active_subscription = (
        db.query(models.SupplierSubscription)
        .filter(models.SupplierSubscription.supplier_id == already_added_supplier.id, models.SupplierSubscription.status == 'active')
        .order_by(models.SupplierSubscription.id.desc())
        .first()
    )
    if has_active_subscription and has_active_subscription.purchase_response:
        subscription_response = check_active_subscription(has_active_subscription.id, db)
        if not subscription_response.get("status"):
            if subscription_response["days_left"] <= 0:
                has_active_subscription.status = "inactive"
        else:
            notification_message = "approved_user_with_subscription"
            email_message = "approved-user-with-subscription"
            days_left = subscription_response["days_left"]

    _create_org_subscription(db, already_added_supplier.id, grant_days, subscription_type)
    return assigned_user.id, days_left, notification_message, email_message


def _notify_assigned_user(background_tasks, db, assigned_user, notification_message, email_message, days_left):
    if assigned_user.fcm_token and assigned_user.send_push_notification == "0":
        user_notification_obj = get_notification_data(notification_message, assigned_user.language_id, db)
        if user_notification_obj:
            notification_body = f'{user_notification_obj["body"].replace("XX", str(days_left))}'
            send_push_notification(
                token=assigned_user.fcm_token,
                title=user_notification_obj["title"],
                body=notification_body,
                platform=decrypt_data(assigned_user.device_type),
                data=user_notification_obj
            )
    user_template_content = get_email_template_content(db, assigned_user.language_id, email_message)
    if user_template_content and assigned_user.send_mail_notification == "0":
        background_tasks.add_task(
            send_subscription_email,
            email=assigned_user.email,
            subject=user_template_content.subject,
            body=user_template_content.body,
            first_name=decrypt_data(assigned_user.first_name),
            last_name=decrypt_data(assigned_user.last_name),
            days_count=days_left,
            selected_language=assigned_user.language.code
        )


@router.post("/upload-excel", dependencies=[Depends(admin_required)])
async def upload_excel(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if not file.filename.endswith((".xlsx", ".xls")):
        return BaseController.errorGeneral("Invalid file type", status.HTTP_400_BAD_REQUEST)

    contents = await file.read()
    df = _parse_excel_file(contents, file.filename)

    col_map = {str(c).strip().lower(): c for c in df.columns}

    def _row_get(row, label):
        actual = col_map.get(label.strip().lower())
        return row.get(actual) if actual is not None else None

    def _parse_yes_no_flag(value, *, default: str) -> str:
        if value is None:
            return default
        s = str(value).strip().lower()
        if s in ('1', 'yes', 'true', 'y'):
            return '1'
        if s in ('0', 'no', 'false', 'n'):
            return '0'
        return default

    def _parse_duration(value) -> int:
        if value is None:
            return 0
        s = str(value).strip()
        if s == '' or s.lower() == 'nan':
            return 0
        try:
            parsed = int(float(s))
        except (ValueError, TypeError):
            return 0
        return max(parsed, 0)

    required_columns = {"Organization Slug", "Organization Identification Number", "Country Name"}
    missing = [c for c in required_columns if c.lower() not in col_map]
    if missing:
        return BaseController.errorGeneral(f"Missing required columns: {missing}", status.HTTP_400_BAD_REQUEST)

    new_log_data_list = []

    for _, row in df.iterrows():
        country_name = _row_get(row, "Country Name")
        slug = _row_get(row, "Organization Slug")
        id_number = _row_get(row, "Organization Identification Number")
        duration = _parse_duration(_row_get(row, "Access Duration"))
        subscription_type = normalize_subscription_type(_row_get(row, "Subscription Type"))

        country = db.query(models.Country).filter(models.Country.country_name == country_name, models.Country.status == "1").first()
        if not country:
            continue

        organization = db.query(models.Organization).filter_by(slug=slug, country_id=country.id).first()
        if not organization:
            continue

        if organization.is_active == "0":
            continue

        existing_user = db.query(models.ApprovedUsers).filter_by(
            organization_id=organization.id,
            organization_identification_number=id_number,
            membership_status='active',
        ).first()
        if existing_user:
            continue

        user_id = None
        days_left = 0
        notification_message = "approved_user"
        email_message = "approved-user"

        assigned_user = db.query(models.User).filter(
            models.User.organization_id == organization.id,
            models.User.organization_document_number_hash == hash_sort_string(str(id_number))
        ).first()

        if assigned_user:
            user_id, days_left, notification_message, email_message = _process_assigned_user(db, assigned_user, organization, duration, subscription_type)

        translation = db.query(models.OrganizationTranslation).join(models.Language).filter(
            models.OrganizationTranslation.organization_id == organization.id,
            models.Language.code == "en"
        ).first()

        discount_raw = _row_get(row, "Discount Eligible")
        discount_eligible = _parse_yes_no_flag(discount_raw, default='1')

        approved_user = models.ApprovedUsers(
            organization_name=translation.org_name if translation else organization.slug,
            organization_identification_number=id_number,
            duration_in_days=duration,
            organization_id=organization.id,
            user_id=user_id if user_id else None,
            subscription_type=subscription_type,
            discount_eligible=discount_eligible,
            membership_status='active' if user_id else 'inactive',
        )
        db.add(approved_user)
        db.flush()

        new_log_data_list.append({
            "approved_id": approved_user.id,
            "organization_name": approved_user.organization_name,
            "organization_identification_number": approved_user.organization_identification_number,
            "duration_in_days": approved_user.duration_in_days,
            "user_id": approved_user.user_id or None,
            "message": "New record added and assigned to user" if approved_user.user_id else "New record added",
            "action_type": "0",
            "organization_id": approved_user.organization_id
        })

        if user_id:
            _notify_assigned_user(background_tasks, db, assigned_user, notification_message, email_message, days_left)

    db.commit()

    for log in new_log_data_list:
        add_approved_user_logs(db, **log)

    return BaseController.success([], "File Uploaded Successfully")


# ──────────────────────────────────────────────
# Pre-Approved Users
# ──────────────────────────────────────────────
@router.get("/pre-approved-users", dependencies=[Depends(admin_required)])
def get_pre_approved_users(
    db: Session = Depends(get_db),
    sort_by: Optional[str] = Query('created_at', description="Field to sort by: 'created_at', 'organization_name', 'duration_in_days'"),
    sort_order: Optional[str] = Query('desc', description="Sort order: 'asc' or 'desc'"),
    search: Optional[str] = Query(None),
    selected_language: Optional[str] = Cookie(default='en'),
    limit: int = Query(default=10, ge=1, le=100, description="Number of records to return"),
    page: int = Query(default=0, ge=0, description="Page number starting from 0"),
):
    sort_column_map = {
        'created_at': models.ApprovedUsers.created_at,
        'organization_name': models.ApprovedUsers.organization_name,
        'duration_in_days': models.ApprovedUsers.duration_in_days,
        'id': models.ApprovedUsers.id,
    }
    sort_col = sort_column_map.get(sort_by, models.ApprovedUsers.created_at)
    sort_clause = asc(sort_col) if sort_order == 'asc' else desc(sort_col)

    base_query = db.query(models.ApprovedUsers).order_by(sort_clause)
    if search:
        search_term = f"%{search}%"
        base_query = base_query.filter(
            or_(
                models.ApprovedUsers.organization_name.ilike(search_term),
                models.ApprovedUsers.organization_identification_number.ilike(search_term),
            )
        )

    total_users = base_query.count()
    user_list = (
        base_query
        .options(
            joinedload(models.ApprovedUsers.organization),
            joinedload(models.ApprovedUsers.user),
        )
        .offset(page * limit)
        .limit(limit)
        .all()
    )

    if not user_list:
        return PaginationResponse(
            total=0,
            page=page,
            per_page=limit,
            data=[],
            message=messages[selected_language]['user_listing'],
        )

    # Batch-fetch subscription state for all linked users in one query (fixes N+1).
    linked_user_ids = [u.user_id for u in user_list if u.user_id]
    subscriptions_by_user = {}
    if linked_user_ids:
        rows = (
            db.query(models.Supplier.user_id, models.SupplierSubscription)
            .join(models.SupplierSubscription, models.SupplierSubscription.supplier_id == models.Supplier.id)
            .filter(
                models.Supplier.user_id.in_(linked_user_ids),
                models.SupplierSubscription.personal_subscription.is_(False),
            )
            .order_by(desc(models.SupplierSubscription.id))
            .all()
        )
        # Keep only the latest subscription per user (rows are sorted desc).
        for user_id, sub in rows:
            if user_id not in subscriptions_by_user:
                subscriptions_by_user[user_id] = sub

    today = datetime.now()
    user_info = []
    for record in user_list:
        subscription_status = "Not Started"
        days_left = record.duration_in_days
        latest_sub = subscriptions_by_user.get(record.user_id) if record.user_id else None
        if latest_sub:
            if latest_sub.status == "inactive":
                subscription_status = "Expired"
                days_left = 0
            elif latest_sub.status == "active":
                subscription_status = "Active"
                if latest_sub.expiry_date and latest_sub.expiry_date >= today:
                    days_left = (latest_sub.expiry_date - today).days
                else:
                    days_left = 0
        elif record.user_id and record.duration_in_days == 0:
            subscription_status = "Expired"
            days_left = 0
        if record.access_removed_at is not None:
            subscription_status = "Expired"
            days_left = 0

        user_name = None
        user_company_name = None
        if record.user:
            user_name = f"{decrypt_data(record.user.first_name)} {decrypt_data(record.user.last_name)}"
            user_company_name = decrypt_data(record.user.company_name)
        elif record.removed_user_display_name:
            user_name = record.removed_user_display_name

        user_info.append({
            "id": record.id,
            "organization_name": record.organization_name,
            "organization_slug": record.organization.slug if record.organization else None,
            "organization_number": record.organization_identification_number,
            "duration_in_days": record.duration_in_days,
            "subscription_type": record.subscription_type,
            "user_name": user_name,
            "user_company_name": user_company_name,
            "organization_id": record.organization_id,
            "user_id": record.user_id,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "subscription_status": subscription_status,
            "days_left": days_left,
            "membership_status": record.membership_status,
            "activation_date": record.activation_date,
            "deactivation_date": record.deactivation_date,
            "deactivation_reason_id": record.deactivation_reason_id,
            "discount_eligible": record.discount_eligible,
            "access_removed_at": record.access_removed_at,
            "member_discounts": _member_discount_matrix(db, record.organization_id) if record.organization_id else [],
        })

    return PaginationResponse(
        total=total_users,
        page=page,
        per_page=limit,
        data=user_info,
        message=messages[selected_language]['user_listing'],
    )

@router.post("/add-approved", dependencies=[Depends(admin_required)])
async def add_approved(
    background_tasks: BackgroundTasks,
    country_id: str = Form(...),
    organization_id: str = Form(...),
    organization_number: str = Form(...),
    duration: str = Form(None),
    user_id: str = Form(None),
    subscription_type: int = Form(0, ge=0, le=3, description="0 = standard, 1 = gold, 2 = silver, 3 = diamond"),
    discount_eligible: str = Form('1', description="'1' = eligible for organization discount, '0' = not eligible. Default '1'."),
    db: Session = Depends(get_db),
):
    try:
        try:
            duration_days = int(str(duration).strip()) if duration not in (None, "") else 0
        except (TypeError, ValueError):
            duration_days = 0
        duration_days = max(duration_days, 0)
        effective_subscription_type = normalize_subscription_type(subscription_type)
        added_country = db.query(models.Country).filter(models.Country.id == country_id, models.Country.status == "1").first()
        if not added_country:
            return BaseController.errorGeneral("Selected country was not found or is currently inactive.", status.HTTP_400_BAD_REQUEST)
        added_organization = (
            db.query(models.Organization)
            .filter(models.Organization.id == organization_id, models.Organization.country_id == country_id, models.Organization.is_active == "1")
            .first()
        )
        if not added_organization:
            return BaseController.errorGeneral("Selected organization was not found or is currently inactive.", status.HTTP_400_BAD_REQUEST)
        already_added_records = (
            db.query(models.ApprovedUsers)
            .filter(
                models.ApprovedUsers.organization_id == added_organization.id,
                models.ApprovedUsers.organization_identification_number == organization_number,
                models.ApprovedUsers.membership_status == 'active',
            )
            .first()
        )
        if already_added_records:
            return BaseController.errorGeneral("Organization with this identification number is already added", status.HTTP_400_BAD_REQUEST)
        already_assigned = db.query(models.User).filter(
            models.User.organization_id == added_organization.id,
            models.User.organization_document_number_hash == hash_sort_string(organization_number)
        ).first()
        if already_assigned and user_id and already_assigned.id != int(user_id):
            return BaseController.errorGeneral("User with this organization identification number is already registered under the selected organization.", status.HTTP_400_BAD_REQUEST)
        elif already_assigned and not user_id:
            user_id = already_assigned.id
        org_translation = (
            db.query(models.OrganizationTranslation)
            .join(models.Language, models.OrganizationTranslation.language_id == models.Language.id)
            .filter(
                models.OrganizationTranslation.organization_id == organization_id,
                models.Language.code == "en"
            )
            .first()
        )
        discount_eligible_value = '1' if str(discount_eligible).strip().lower() in ('1', 'yes', 'true', 'y') else '0'
        new_data = models.ApprovedUsers(
            organization_name=org_translation.org_name if org_translation else added_organization.slug,
            organization_identification_number=organization_number,
            duration_in_days=duration_days,
            organization_id=organization_id,
            subscription_type=effective_subscription_type,
            discount_eligible=discount_eligible_value,
        )
        if user_id:
            user_already_assigned = (
                db.query(models.ApprovedUsers)
                .filter(
                    models.ApprovedUsers.user_id == user_id,
                    models.ApprovedUsers.membership_status == 'active',
                )
                .first()
            )
            if user_already_assigned:
                return BaseController.errorGeneral("This user is already assigned to other organization", status.HTTP_400_BAD_REQUEST)
            already_added_supplier = (
                db.query(models.Supplier)
                .filter(models.Supplier.user_id==user_id)
                .first()
            )
            if already_added_supplier:
                existing_schedule_entries = db.query(models.SupplierItemSchedule).filter(
                    models.SupplierItemSchedule.supplier_id == already_added_supplier.id
                ).first()
                if existing_schedule_entries and duration_days > 0:
                    has_active_subscription = (
                        db.query(models.SupplierSubscription)
                        .filter(models.SupplierSubscription.supplier_id==already_added_supplier.id, models.SupplierSubscription.status=='active')
                        .order_by(models.SupplierSubscription.id.desc())
                        .first()
                    )
                    days_left = 0
                    notification_message = "approved_user"
                    email_message = "approved-user"
                    if has_active_subscription and has_active_subscription.purchase_response:
                        subscription_response = check_active_subscription(has_active_subscription.id, db)
                        if not subscription_response.get("status"):
                            if subscription_response["days_left"] <= 0:
                                has_active_subscription.status = "inactive"
                        else:
                            notification_message = "approved_user_with_subscription"
                            email_message = "approved-user-with-subscription"
                            days_left = subscription_response.get("days_left")
                    expiry_date = datetime.now() + timedelta(days=duration_days)
                    new_subscription = models.SupplierSubscription(
                        supplier_id=already_added_supplier.id,
                        order_id='',
                        product_id='',
                        purchase_time='',
                        purchase_state=1,
                        purchase_token='',
                        quantity=1,
                        status='active',
                        subscription_type=effective_subscription_type,
                        expiry_date=expiry_date,
                        personal_subscription=False,
                        personal_subscription_pending_days=0,
                        provider='org_grant',
                    )
                    db.add(new_subscription)
                    if already_added_supplier.user.fcm_token and already_added_supplier.user.send_push_notification == "0":
                        user_notification_obj = get_notification_data(notification_message, already_added_supplier.user.language_id, db)
                        if user_notification_obj:
                            notification_body = f'{user_notification_obj["body"].replace("XX", str(days_left))}'
                            send_push_notification(
                                token=already_added_supplier.user.fcm_token,
                                title=user_notification_obj["title"],
                                body=notification_body,
                                platform=decrypt_data(already_added_supplier.user.device_type),
                                data=user_notification_obj
                            )
                    user_template_content = get_email_template_content(db, already_added_supplier.user.language_id, email_message)
                    if user_template_content and already_added_supplier.user.send_mail_notification == "0":
                        background_tasks.add_task(
                            send_subscription_email,
                            email=already_added_supplier.user.email,
                            subject=user_template_content.subject,
                            body=user_template_content.body,
                            first_name=decrypt_data(already_added_supplier.user.first_name),
                            last_name=decrypt_data(already_added_supplier.user.last_name),
                            days_count=days_left,
                            selected_language=already_added_supplier.user.language.code
                        )
            new_data.user_id = int(user_id)
        if not new_data.user_id:
            new_data.membership_status = 'inactive'
        db.add(new_data)
        db.commit()
        db.refresh(new_data)
        if user_id:
            from app.utils.org_membership import stamp_activation
            stamp_activation(db, new_data)
            new_data.user.organization_id = added_organization.id
            new_data.user.organization_document_number = encrypt_data(organization_number)
            new_data.user.organization_document_number_hash = hash_sort_string(organization_number)
            new_data.user.organization_document_img = None
            db.commit()
        new_log_data = {
            "approved_id": new_data.id,
            "organization_name": new_data.organization_name,
            "organization_identification_number": new_data.organization_identification_number,
            "duration_in_days": new_data.duration_in_days,
            "user_id": new_data.user_id if new_data.user_id else None,
            "message": "New record added and assigned to user" if new_data.user_id else "New record added",
            "action_type": "0",
            "organization_id": new_data.organization_id
        }
        add_approved_user_logs(db, **new_log_data)

        return BaseController.success([], "Record Added Successfully")

    except Exception as e:
        return BaseController.errorGeneral("Unable to add record", status.HTTP_400_BAD_REQUEST)


@router.get("/approved-user-info/{record_id}", dependencies=[Depends(admin_required)])
def get_approved_user_info(
    record_id: int,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    approved_user = db.query(models.ApprovedUsers).filter(models.ApprovedUsers.id == record_id).first()
    
    if not approved_user:
        return BaseController.errorGeneral("No record found.", status.HTTP_404_NOT_FOUND)
    user_info = {
        "id": approved_user.id,
        "organization_name": approved_user.organization_name,
        "organization_number": approved_user.organization_identification_number,
        "duration_in_days": approved_user.duration_in_days,
        "subscription_type": approved_user.subscription_type,
        "user_name": (
            f"{decrypt_data(approved_user.user.first_name)} {decrypt_data(approved_user.user.last_name)}"
            if approved_user.user
            else approved_user.removed_user_display_name
        ),
        "organization_id": approved_user.organization_id,
        "country_id": approved_user.organization.country_id if approved_user.organization else None,
        "user_id": approved_user.user_id,
        "created_at": approved_user.created_at,
        "updated_at": approved_user.updated_at,
        "membership_status": approved_user.membership_status,
        "activation_date": approved_user.activation_date,
        "deactivation_date": approved_user.deactivation_date,
        "deactivation_reason_id": approved_user.deactivation_reason_id,
        "discount_eligible": approved_user.discount_eligible,
        "organization_is_active": approved_user.organization.is_active if approved_user.organization else None,
        "access_removed_at": approved_user.access_removed_at,
        "has_active_member_for_identifier": (
            db.query(models.ApprovedUsers.id)
            .filter(
                models.ApprovedUsers.organization_id == approved_user.organization_id,
                models.ApprovedUsers.organization_identification_number == approved_user.organization_identification_number,
                models.ApprovedUsers.membership_status == 'active',
            )
            .first()
            is not None
        ) if approved_user.organization_id else False,
        "has_removed_access_for_identifier": (
            db.query(models.ApprovedUsers.id)
            .filter(
                models.ApprovedUsers.removed_user_id == approved_user.user_id,
                models.ApprovedUsers.organization_id == approved_user.organization_id,
                models.ApprovedUsers.organization_identification_number == approved_user.organization_identification_number,
                models.ApprovedUsers.access_removed_at.isnot(None),
                models.ApprovedUsers.access_removed_at > approved_user.created_at,
                models.ApprovedUsers.id != approved_user.id,
            )
            .first()
            is not None
        ) if (approved_user.user_id and approved_user.organization_id) else False,
        "member_discounts": _member_discount_matrix(db, approved_user.organization_id) if approved_user.organization_id else [],
    }
    orgTransAlias = models.OrganizationTranslation
    langDetailAlias = models.Language
    org_translations = db.query(orgTransAlias, langDetailAlias).outerjoin(langDetailAlias, langDetailAlias.id == orgTransAlias.language_id).filter(
        models.OrganizationTranslation.organization_id == approved_user.organization_id
    ).all()
    translations = []
    for orgTrans, langDetail in org_translations:
        translations.append({
            'id': orgTrans.id,
            'org_name': orgTrans.org_name,
            'language_detail': {
                'id':langDetail.id,
                'name':langDetail.name,
                'code':langDetail.code,
                'icon':langDetail.icon,
            } if langDetail else None
        })
    approved_logs = (
        db.query(models.ApprovedUsersLogs)
        .filter(models.ApprovedUsersLogs.approved_user_id == record_id)
        .all()
    )
    approved_logs_list = []
    for log in approved_logs:
        approved_logs_list.append({
            "id": log.id,
            "approved_user_id": log.approved_user_id,
            "organization_name": log.organization_name,
            "organization_identification_number": log.organization_identification_number,
            "duration_in_days": log.duration_in_days,
            "organization_id": log.organization_id,
            "user_id": log.user_id,
            "message": log.message,
            "action_type": log.action_type,
            "created_at": log.created_at,
            "first_name": decrypt_data(log.user.first_name) if log.user else None,
            "last_name": decrypt_data(log.user.last_name) if log.user else None,
            "email": log.user.email if log.user else None
        })
    approved_user_info = {}
    user_id = approved_user.user_id if approved_user.user_id else None
    if user_id:
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if not user:
            return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)
        
        supplier = db.query(models.Supplier).filter(models.Supplier.user_id == user.id).first()
        
        if supplier:

            total_supplier_reviews, total_average_reviews = (
                db.query(
                    func.count(models.t_booking_reviews.c.receiver_id),
                    coalesce(func.avg(models.t_booking_reviews.c.rating_value), 0)
                )
                .filter(models.t_booking_reviews.c.receiver_id == supplier.user.id, models.t_booking_reviews.c.reviewer_type == "1")
                .first()
            )
            total_customer_reviews = (
                db.query(func.count(models.t_booking_reviews.c.sender_id))
                .filter(models.t_booking_reviews.c.receiver_id == supplier.user.id, models.t_booking_reviews.c.reviewer_type == "0")
                .scalar()
            )
            
        else:
            total_supplier_reviews = total_average_reviews = 0
            total_customer_reviews, total_average_reviews = (
                db.query(
                    func.count(models.t_booking_reviews.c.receiver_id),
                    coalesce(func.avg(models.t_booking_reviews.c.rating_value), 0)
                )
                .filter(models.t_booking_reviews.c.receiver_id == user.id, models.t_booking_reviews.c.reviewer_type == "0")
                .first()
            )
        language_id = (
            db.query(models.Language.id)
            .filter(models.Language.code == selected_language)
            .scalar() or 1
        )         
        default_lang_org_name = None
        if user.organization_id:
            default_lang_org_name = db.query(models.OrganizationTranslation).filter(models.OrganizationTranslation.organization_id == user.organization_id, models.OrganizationTranslation.language_id == language_id).first()

        approved_user_info = admin.ApprovedUserInfoAdminModel(
            id=supplier.user_id if supplier else user.id,
            first_name=decrypt_data(supplier.user.first_name) if supplier else decrypt_data(user.first_name),
            last_name=decrypt_data(supplier.user.last_name) if supplier else decrypt_data(user.last_name),
            information=decrypt_data(supplier.user.information) if supplier else decrypt_data(user.information),
            email_verified_at=user.email_verified_at,
            about=user.about,
            email=user.email,
            user_status = user.status,
            status_reason = messages[selected_language].get(user.status_reason, user.status_reason),
            company_name = decrypt_data(user.company_name),
            gender = user.gender,
            time_zone = decrypt_data(user.time_zone),
            organization_id = user.organization_id if user.organization else None,
            organization_slug = default_lang_org_name.org_name if default_lang_org_name else None,
            organization_document_number = decrypt_data(user.organization_document_number) if user.organization else None,
            organization_document_img = user.organization_document_img if user.organization else None,
            selected_language=user.language.name,
            selected_language_icon=user.language.icon,
            ip_address = decrypt_data(user.ip_address),
            nationality = user.nationality.country_name if user.nationality else None,
            currency = user.currency.name if user.currency else None,
            created_at = user.created_at if user.created_at else None,
            updated_at = user.updated_at if user.updated_at else None,
            deleted_at = user.deleted_at if user.deleted_at else None,
            address=decrypt_data(supplier.user.address) if supplier else decrypt_data(user.address),
            city=decrypt_data(supplier.user.city) if supplier else decrypt_data(user.city),
            country_id=supplier.user.country_id if supplier else user.country_id,
            country_name=user.country.country_name if user.country else None,
            apartment_zone=decrypt_data(supplier.user.apartment_zone) if supplier else decrypt_data(user.apartment_zone),
            additional_direction=decrypt_data(supplier.user.additional_direction) if supplier else decrypt_data(user.additional_direction),
            is_supplier=True if supplier else False,
            supplier_id=supplier.id if supplier else None,
            supplier_status=supplier.status if supplier else None,
            supplier_rejection_reason=supplier.rejection_reason if supplier else None,
            total_review_as_customer=total_customer_reviews,
            total_review_as_supplier=total_supplier_reviews,
            supplier_image=supplier.user.profile_img if supplier else None,
            customer_image=user.profile_img,
            is_customer_profile_completed=True if user.status=="Approved" else False,
            total_average_reviews=total_average_reviews,
            latitude=decrypt_data(supplier.user.latitude) if supplier else decrypt_data(user.latitude),
            longitude=decrypt_data(supplier.user.longitude) if supplier else decrypt_data(user.longitude),
            supplier_nationality_flag = supplier.user.country.country_icon if supplier and supplier.user.country else None,
            customer_nationality_flag = user.country.country_icon if user.country else None,
            supplier_user_status = supplier.user.status if supplier else None,
            supplier_user_status_reason = messages[selected_language].get(supplier.user.status_reason, supplier.user.status_reason) if supplier else None,
            document_verification_status = user.document_verification_status,
            document_verification_reason = user.document_verification_reason
        )
    response_payload = admin.ApprovedUserInfoResponse(
        user_info=admin.UserInfoModel(**user_info),
        approved_user_info=jsonable_encoder(approved_user_info) if approved_user_info else None,
        translations=translations,
        approved_logs=approved_logs_list
    )
    return BaseController.success(jsonable_encoder(response_payload), "Approved user info retrieved successfully")


@router.put("/update-approved/{record_id}", dependencies=[Depends(admin_required)])
async def update_approved(
    record_id: int,
    background_tasks: BackgroundTasks,
    country_id: str = Form(None),
    organization_id: str = Form(None),
    organization_number: str = Form(None),
    duration: str = Form(None),
    user_id: str = Form(None),
    subscription_type: Optional[int] = Form(None, ge=0, le=3, description="Optional: 0 = standard, 1 = gold, 2 = silver, 3 = diamond"),
    discount_eligible: Optional[str] = Form(None, description="Optional: '0' or '1'"),
    db: Session = Depends(get_db),
):
    try:
        current_record = db.query(models.ApprovedUsers).filter(models.ApprovedUsers.id == record_id).first()
        if not current_record:
            return BaseController.errorGeneral("Record not found", status.HTTP_400_BAD_REQUEST)
        if discount_eligible in ('0', '1'):
            current_record.discount_eligible = discount_eligible

        # Resolve effective subscription type once: use new value if provided, otherwise keep existing
        effective_subscription_type = (
            normalize_subscription_type(subscription_type)
            if subscription_type is not None
            else current_record.subscription_type
        )

        added_country = db.query(models.Country).filter(models.Country.id == country_id, models.Country.status == "1").first()
        if not added_country:
            return BaseController.errorGeneral("Selected country was not found or is currently inactive.", status.HTTP_400_BAD_REQUEST)
        current_record_user_id = current_record.user_id
        if organization_id and organization_number:
            try:
                organization_id_int = int(organization_id)
            except (TypeError, ValueError):
                organization_id_int = None
            organization_changed = (
                organization_id_int is None
                or organization_id_int != current_record.organization_id
                or organization_number != current_record.organization_identification_number
            )
            if organization_changed:
                added_organization = (
                    db.query(models.Organization)
                    .filter(models.Organization.id == organization_id, models.Organization.country_id == country_id, models.Organization.is_active == "1")
                    .first()
                )
                if not added_organization:
                    return BaseController.errorGeneral("Selected organization was not found or is currently inactive", status.HTTP_400_BAD_REQUEST)
                already_added_records = (
                    db.query(models.ApprovedUsers)
                    .filter(
                        models.ApprovedUsers.organization_id == added_organization.id,
                        models.ApprovedUsers.organization_identification_number == organization_number,
                        models.ApprovedUsers.membership_status == 'active',
                        models.ApprovedUsers.id != record_id,
                    )
                    .first()
                )
                if already_added_records:
                    return BaseController.errorGeneral("Organization with this identification number is already added", status.HTTP_400_BAD_REQUEST)
                already_assigned = db.query(models.User).filter(
                    models.User.organization_id == added_organization.id,
                    models.User.organization_document_number_hash == hash_sort_string(organization_number)
                ).first()
                if already_assigned and user_id and already_assigned.id != int(user_id):
                    return BaseController.errorGeneral("User with this organization identification number is already registered under the selected organization.", status.HTTP_400_BAD_REQUEST)
                elif already_assigned and not user_id:
                    user_id = already_assigned.id
                org_translation = (
                    db.query(models.OrganizationTranslation)
                    .join(models.Language, models.OrganizationTranslation.language_id == models.Language.id)
                    .filter(
                        models.OrganizationTranslation.organization_id == organization_id,
                        models.Language.code == "en"
                    )
                    .first()
                )
                current_record.organization_name = org_translation.org_name if org_translation else added_organization.slug
                current_record.organization_identification_number = organization_number
                current_record.organization_id=organization_id
        user_updated = False
        if user_id and not current_record.user_id:
            org_for_assignment = (
                db.query(models.Organization)
                .filter(models.Organization.id == current_record.organization_id)
                .first()
            )
            if org_for_assignment is None or org_for_assignment.is_active != '1':
                return BaseController.errorGeneral(
                    "Cannot assign a user: the organization is inactive.",
                    status.HTTP_409_CONFLICT,
                )
            user_already_assigned = (
                db.query(models.ApprovedUsers)
                .filter(
                    models.ApprovedUsers.user_id == user_id,
                    models.ApprovedUsers.membership_status == 'active',
                )
                .first()
            )
            if user_already_assigned:
                return BaseController.errorGeneral("This user is already assigned to other organization", status.HTTP_400_BAD_REQUEST)

            already_added_supplier = (
                db.query(models.Supplier)
                .filter(models.Supplier.user_id==user_id)
                .first()
            )
            if already_added_supplier:
                existing_schedule_entries = db.query(models.SupplierItemSchedule).filter(
                    models.SupplierItemSchedule.supplier_id == already_added_supplier.id
                ).first()
                grant_days = int(duration or 0)
                if existing_schedule_entries and grant_days > 0:
                    has_active_subscription = (
                        db.query(models.SupplierSubscription)
                        .filter(models.SupplierSubscription.supplier_id==already_added_supplier.id, models.SupplierSubscription.status=='active')
                        .order_by(models.SupplierSubscription.id.desc())
                        .first()
                    )
                    days_left = 0
                    notification_message = "approved_user"
                    email_message = "approved-user"
                    if has_active_subscription and has_active_subscription.purchase_response:
                        subscription_response = check_active_subscription(has_active_subscription.id, db)
                        if not subscription_response.get("status"):
                            if subscription_response["days_left"] <= 0:
                                has_active_subscription.status = "inactive"
                        else:
                            notification_message = "approved_user_with_subscription"
                            email_message = "approved-user-with-subscription"
                            days_left = subscription_response.get("days_left")
                    expiry_date = datetime.now() + timedelta(days=grant_days)
                    new_subscription = models.SupplierSubscription(
                        supplier_id=already_added_supplier.id,
                        order_id='',
                        product_id='',
                        purchase_time='',
                        purchase_state=1,
                        purchase_token='',
                        quantity=1,
                        status='active',
                        subscription_type=effective_subscription_type,
                        expiry_date=expiry_date,
                        personal_subscription=False,
                        personal_subscription_pending_days=0,
                        provider='org_grant',
                    )
                    db.add(new_subscription)
                    if already_added_supplier.user.fcm_token and already_added_supplier.user.send_push_notification == "0":
                        user_notification_obj = get_notification_data(notification_message, already_added_supplier.user.language_id, db)
                        if user_notification_obj:
                            notification_body = f'{user_notification_obj["body"].replace("XX", str(days_left))}'
                            send_push_notification(
                                token=already_added_supplier.user.fcm_token,
                                title=user_notification_obj["title"],
                                body=notification_body,
                                platform=decrypt_data(already_added_supplier.user.device_type),
                                data=user_notification_obj
                            )
                    user_template_content = get_email_template_content(db, already_added_supplier.user.language_id, email_message)
                    if user_template_content and already_added_supplier.user.send_mail_notification == "0":
                        background_tasks.add_task(
                            send_subscription_email,
                            email=already_added_supplier.user.email,
                            subject=user_template_content.subject,
                            body=user_template_content.body,
                            first_name=decrypt_data(already_added_supplier.user.first_name),
                            last_name=decrypt_data(already_added_supplier.user.last_name),
                            days_count=days_left,
                            selected_language=already_added_supplier.user.language.code
                        )
            current_record.user_id=user_id
            current_record.membership_status = 'active'
            user_updated = True
        if duration is not None:
            try:
                new_duration = int(str(duration).strip()) if str(duration).strip() != "" else 0
            except (TypeError, ValueError):
                new_duration = 0
            new_duration = max(new_duration, 0)
            if new_duration != current_record.duration_in_days:
                supplier = (
                    db.query(models.Supplier)
                    .join(models.User, models.Supplier.user_id == models.User.id)
                    .filter(models.User.id == current_record_user_id)
                    .first()
                )
                if supplier:
                    supplier_subscription = (
                        db.query(models.SupplierSubscription)
                        .filter(
                            models.SupplierSubscription.supplier_id == supplier.id,
                            models.SupplierSubscription.status == "active",
                            models.SupplierSubscription.purchase_response.is_(None),
                            models.SupplierSubscription.personal_subscription == False
                        )
                        .order_by(models.SupplierSubscription.id.desc())
                        .first()
                    )
                    if supplier_subscription:
                        if new_duration == 0:
                            supplier_subscription.status = "inactive"
                            supplier_subscription.expiry_date = datetime.now()
                        else:
                            old_duration = current_record.duration_in_days
                            day_difference = abs(new_duration - old_duration)
                            if new_duration < old_duration:
                                expiry_date = supplier_subscription.expiry_date - timedelta(days=day_difference)
                            else:
                                expiry_date = supplier_subscription.expiry_date + timedelta(days=day_difference)
                            supplier_subscription.expiry_date = expiry_date
                            if expiry_date < datetime.now():
                                supplier_subscription.status = "inactive"

                current_record.duration_in_days = new_duration

        if subscription_type is not None:
            current_record.subscription_type = normalize_subscription_type(subscription_type)
            # Propagate the tier change to the active org-granted SupplierSubscription
            # so downstream reads (admin/user-info, current_selected_plan_type, etc.) reflect it.
            if current_record_user_id:
                org_supplier = (
                    db.query(models.Supplier)
                    .filter(models.Supplier.user_id == current_record_user_id)
                    .first()
                )
                if org_supplier:
                    active_org_subscription = (
                        db.query(models.SupplierSubscription)
                        .filter(
                            models.SupplierSubscription.supplier_id == org_supplier.id,
                            models.SupplierSubscription.status == "active",
                            models.SupplierSubscription.purchase_response.is_(None),
                            models.SupplierSubscription.personal_subscription == False,
                        )
                        .order_by(models.SupplierSubscription.id.desc())
                        .first()
                    )
                    if active_org_subscription:
                        active_org_subscription.subscription_type = current_record.subscription_type
        db.commit()
        if user_updated:
            from app.utils.org_membership import stamp_activation
            stamp_activation(db, current_record)
            current_record.user.organization_id = current_record.organization_id
            current_record.user.organization_document_number = encrypt_data(organization_number)
            current_record.user.organization_document_number_hash = hash_sort_string(organization_number)
            current_record.user.organization_document_img = None
            db.commit()
        new_log_data = {
            "approved_id": current_record.id,
            "organization_name": current_record.organization_name,
            "organization_identification_number": current_record.organization_identification_number,
            "duration_in_days": current_record.duration_in_days,
            "user_id": current_record.user_id if current_record.user_id else None,
            "message": "User assigned by admin" if user_updated else "Record updated",
            "action_type": "2" if user_updated else "1",
            "organization_id": current_record.organization_id
        }
        add_approved_user_logs(db, **new_log_data)

        return BaseController.success([], "Record Updated Successfully")

    except Exception as e:
        logger.exception("Unable to update record")
        return BaseController.errorGeneral("Unable to update record", status.HTTP_400_BAD_REQUEST)

@router.put("/update-approved-duration/{record_id}", dependencies=[Depends(admin_required)])
async def update_approved_duration(
    record_id: int,
    duration: str = Form(None),
    db: Session = Depends(get_db),
):
    try:
        current_record = db.query(models.ApprovedUsers).filter(models.ApprovedUsers.id == record_id).first()
        if not current_record:
            return BaseController.errorGeneral("Record not found", status.HTTP_400_BAD_REQUEST)
        if duration is not None:
            try:
                new_duration = int(str(duration).strip()) if str(duration).strip() != "" else 0
            except (TypeError, ValueError):
                new_duration = 0
            new_duration = max(new_duration, 0)
            if new_duration != current_record.duration_in_days:
                supplier = (
                    db.query(models.Supplier)
                    .join(models.User, models.Supplier.user_id == models.User.id)
                    .filter(models.User.id == current_record.user_id)
                    .first()
                )
                if supplier:
                    supplier_subscription = (
                        db.query(models.SupplierSubscription)
                        .filter(
                            models.SupplierSubscription.supplier_id == supplier.id,
                            models.SupplierSubscription.status == "active",
                            models.SupplierSubscription.purchase_response.is_(None),
                            models.SupplierSubscription.personal_subscription == False
                        )
                        .order_by(models.SupplierSubscription.id.desc())
                        .first()
                    )
                    if supplier_subscription:
                        if new_duration == 0:
                            supplier_subscription.status = "inactive"
                            supplier_subscription.expiry_date = datetime.now()
                        else:
                            old_duration = current_record.duration_in_days
                            day_difference = abs(new_duration - old_duration)
                            if new_duration < old_duration:
                                supplier_subscription.expiry_date -= timedelta(days=day_difference)
                            else:
                                supplier_subscription.expiry_date += timedelta(days=day_difference)
                            if supplier_subscription.expiry_date < datetime.now():
                                supplier_subscription.status = "inactive"
                current_record.duration_in_days = new_duration
        db.commit()

        return BaseController.success([], "Record Updated Successfully")

    except Exception as e:
        logger.exception("Unable to update record")
        return BaseController.errorGeneral("Unable to update record", status.HTTP_400_BAD_REQUEST)

@router.get("/all-users", dependencies=[Depends(admin_required)])
def get_all_users(
    db: Session = Depends(get_db),
):
    approved_user_ids_subq = (
        db.query(models.ApprovedUsers.user_id)
        .filter(models.ApprovedUsers.user_id.isnot(None))
        .subquery()
    )
    all_users_list = (
        db.query(models.User)
        .join(models.Role, models.Role.id == models.User.role_id)
        .filter(models.User.status != "Deleted")
        .filter(models.Role.guard_name == 'user')
        .filter(models.User.id.notin_(approved_user_ids_subq))
        .all()
    )
    if not all_users_list:
        return BaseController.success([], "User list retrieved successfully")
    user_data = []
    for user in all_users_list:
        user_data.append(
            admin.GetAllUsersList(
                id=user.id,
                email=user.email,
                first_name=decrypt_data(user.first_name),
                last_name=decrypt_data(user.last_name),
                company_name=decrypt_data(user.company_name),
            )
        )

    return BaseController.success(user_data, "User list retrieved successfully")

@router.get("/all-country-users/{country_id}", dependencies=[Depends(admin_required)])
def get_all_country_users(
    country_id: int,
    user_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
):
    approved_user_ids_subq = (
        db.query(models.ApprovedUsers.user_id)
        .filter(models.ApprovedUsers.user_id.isnot(None))
        .subquery()
    )
    query = (
        db.query(models.User)
        .join(models.Role, models.Role.id == models.User.role_id)
        .filter(models.User.status != "Deleted")
        .filter(or_(models.User.country_id == country_id, models.User.country_id.is_(None)))
        .filter(models.Role.guard_name == 'user')
    )
    if user_id is not None:
        query = query.filter(or_(
            models.User.id.notin_(approved_user_ids_subq),
            models.User.id == user_id
        ))
    else:
        query = query.filter(models.User.id.notin_(approved_user_ids_subq))

    all_users_list = query.all()
    if not all_users_list:
        return BaseController.success([], "User list retrieved successfully")
    user_data = []
    for user in all_users_list:
        user_data.append(
            admin.GetAllUsersList(
                id=user.id,
                email=user.email,
                first_name=decrypt_data(user.first_name),
                last_name=decrypt_data(user.last_name),
                company_name=decrypt_data(user.company_name),
            )
        )

    return BaseController.success(user_data, "User list retrieved successfully")

@router.get("/country-organizations/{country_id}", dependencies=[Depends(admin_required)])
def get_country_organizations(
    country_id: int,
    include_inactive: bool = Query(default=True),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en')
):
    language_id = (
            db.query(models.Language.id)
            .filter(models.Language.code == selected_language)
            .scalar() or 1
        )
    
    organizations = db.query(models.Organization).filter(models.Organization.country_id == country_id).all()
    org_query = db.query(models.Organization).filter(models.Organization.country_id == country_id)
    if not include_inactive:
        org_query = org_query.filter(models.Organization.is_active == "1")
    
    organizations = org_query.all()

    subscription_data = get_added_subscriptions() or {}
    subscription_plans_picker = _group_subscriptions_by_plan(subscription_data)

    org_list = []
    for organization in organizations:
        default_lang_subject = db.query(models.OrganizationTranslation).filter(models.OrganizationTranslation.organization_id == organization.id, models.OrganizationTranslation.language_id == language_id).first()

        org_list.append({
            'id': organization.id,
            'slug': organization.slug,
            'name': default_lang_subject.org_name if default_lang_subject else None,
            'is_active': organization.is_active,
            'country_id': organization.country_id,
            'country_name': organization.country.country_name,
            'created_at': jsonable_encoder(organization.created_at),
            'has_document': organization.has_document,
            'responsible_person_name': organization.responsible_person_name,
            'responsible_person_email': organization.responsible_person_email,
            'free_trial_days': organization.free_trial_days,
            'member_discounts': _member_discount_matrix(db, organization.id),
            'active_member_count': _count_active_members(db, organization.id),
            'subscription_plans_picker': subscription_plans_picker,
        })

    return BaseController.success(org_list, "Organization list") if org_list else BaseController.success([], "No organizations found.")


# ──────────────────────────────────────────────
# Referral Settings
# ──────────────────────────────────────────────
@router.get("/referral-settings", dependencies=[Depends(admin_required)])
def get_referral_settings(
    db: Session = Depends(get_db),
    selected_language: Optional[str]=Cookie(default='en')
):
    try:
        referral_settings = (
            db.query(models.ReferralSettings)
            .order_by(models.ReferralSettings.subscription_type)
            .all()
        )
        payload = [
            jsonable_encoder(admin.GetReferralSettings.model_validate(r))
            for r in referral_settings
        ]
        return BaseController.success(payload, messages[selected_language]['referral_settings_retrieved'])

    except Exception as e:
        return BaseController.errorGeneral("Unable to retrieve referral settings", status.HTTP_400_BAD_REQUEST)


_REFERRAL_POINTS_MAX = Decimal("1000000.00")


def _quantize_referral_points(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@router.put("/update-referral-setting/{setting_id}", dependencies=[Depends(admin_required)])
async def update_referral_setting(
    setting_id: int,
    total_annual_subscription_points: float = Form(..., ge=0, le=float(_REFERRAL_POINTS_MAX)),
    total_monthly_subscription_points: float = Form(..., ge=0, le=float(_REFERRAL_POINTS_MAX)),
    is_monthly_points_enabled: bool = Form(...),
    db: Session = Depends(get_db),
):
    referral_setting = db.query(models.ReferralSettings).filter(models.ReferralSettings.id == setting_id).first()
    if not referral_setting:
        return BaseController.errorGeneral('Referral settings not found', 400)
    referral_setting.total_annual_subscription_points = _quantize_referral_points(total_annual_subscription_points)
    referral_setting.total_monthly_subscription_points = _quantize_referral_points(total_monthly_subscription_points)
    referral_setting.is_monthly_points_enabled = is_monthly_points_enabled
    db.commit()

    return BaseController.success(jsonable_encoder(admin.GetReferralSettings.model_validate(referral_setting)), "Referral settings updated successfully")

@router.post("/add-referral-setting", dependencies=[Depends(admin_required)])
async def add_referral_setting(
    total_annual_subscription_points: float = Form(..., ge=0, le=float(_REFERRAL_POINTS_MAX)),
    total_monthly_subscription_points: float = Form(..., ge=0, le=float(_REFERRAL_POINTS_MAX)),
    is_monthly_points_enabled: bool = Form(...),
    subscription_type: int = Form(0, ge=0, le=3, description="Plan tier — 0 = standard, 1 = gold, 2 = silver, 3 = diamond"),
    db: Session = Depends(get_db),
):
    tier = normalize_subscription_type(subscription_type)
    existing = (
        db.query(models.ReferralSettings)
        .filter(models.ReferralSettings.subscription_type == tier)
        .first()
    )
    if existing:
        return BaseController.errorGeneral("Referral setting already added for this subscription type", status.HTTP_400_BAD_REQUEST)
    new_setting = models.ReferralSettings(
        subscription_type=tier,
        total_annual_subscription_points=_quantize_referral_points(total_annual_subscription_points),
        total_monthly_subscription_points=_quantize_referral_points(total_monthly_subscription_points),
        is_monthly_points_enabled=is_monthly_points_enabled,
    )
    db.add(new_setting)
    db.commit()

    return BaseController.success(jsonable_encoder(admin.GetReferralSettings.model_validate(new_setting)), "Referral settings created successfully")


@router.post("/pre-approved-users/{record_id}/deactivate", dependencies=[Depends(admin_required)])
def deactivate_approved_user(
    record_id: int,
    payload: admin.DeactivateApprovedUser,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en'),
):
    """Mark an org member inactive.

    Cuts any running org-granted supplier subscription immediately, stamps
    deactivation metadata on the row, and appends a Deactivated log entry.
    Already-purchased Apple/Google subscriptions are untouched.
    """
    from app.utils.org_membership import deactivate as deactivate_member

    row = db.query(models.ApprovedUsers).filter(models.ApprovedUsers.id == record_id).first()
    if not row:
        return BaseController.errorGeneral("Pre-approved user record not found.", status.HTTP_404_NOT_FOUND)
    if row.membership_status == 'inactive':
        return BaseController.errorGeneral("This member is already inactive.", status.HTTP_409_CONFLICT)

    reason = (
        db.query(models.DeactivationReason)
        .filter(models.DeactivationReason.id == payload.reason_id, models.DeactivationReason.status == '1')
        .first()
    )
    if not reason:
        return BaseController.errorGeneral(
            "Selected deactivation reason was not found.", status.HTTP_400_BAD_REQUEST,
        )

    token_data = verify_access_token(token)
    actor = db.query(models.User).filter(models.User.email == token_data.email).first()
    deactivate_member(db, row, actor_id=(actor.id if actor else None), reason_id=reason.id)
    db.commit()
    return BaseController.success([], "Member deactivated.")


@router.delete("/pre-approved-users/{record_id}/remove-access", dependencies=[Depends(admin_required)])
def remove_approved_user_access(
    record_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
):
    """Mark the users link as removed on an inactive row and clone an empty slot for the same organization identifier."""
    row = db.query(models.ApprovedUsers).filter(models.ApprovedUsers.id == record_id).first()
    if not row:
        return BaseController.errorGeneral("Pre-approved user record not found.", status.HTTP_404_NOT_FOUND)
    if row.membership_status != 'inactive':
        return BaseController.errorGeneral(
            "Access can only be removed after the membership is deactivated.",
            status.HTTP_409_CONFLICT,
        )
    if not row.user_id:
        return BaseController.errorGeneral("This record has no user assigned.", status.HTTP_409_CONFLICT)
    if row.access_removed_at is not None:
        return BaseController.errorGeneral("User access has already been removed on this record.", status.HTTP_409_CONFLICT)

    active_member_for_identifier = (
        db.query(models.ApprovedUsers.id)
        .filter(
            models.ApprovedUsers.user_id == row.user_id,
            models.ApprovedUsers.organization_id == row.organization_id,
            models.ApprovedUsers.organization_identification_number == row.organization_identification_number,
            models.ApprovedUsers.membership_status == 'active',
        )
        .first()
    )
    if active_member_for_identifier is not None:
        return BaseController.errorGeneral(
            "This user already has an active membership under the same organization identifier — remove access is not applicable.",
            status.HTTP_409_CONFLICT,
        )

    already_removed_for_identifier = (
        db.query(models.ApprovedUsers.id)
        .filter(
            models.ApprovedUsers.removed_user_id == row.user_id,
            models.ApprovedUsers.organization_id == row.organization_id,
            models.ApprovedUsers.organization_identification_number == row.organization_identification_number,
            models.ApprovedUsers.access_removed_at.isnot(None),
            models.ApprovedUsers.access_removed_at > row.created_at,
            models.ApprovedUsers.id != row.id,
        )
        .first()
    )
    if already_removed_for_identifier is not None:
        return BaseController.errorGeneral(
            "User access has already been removed for this organization identifier on another record.",
            status.HTTP_409_CONFLICT,
        )

    token_data = verify_access_token(token)
    actor = db.query(models.User).filter(models.User.email == token_data.email).first()

    linked_user = db.query(models.User).filter(models.User.id == row.user_id).first()
    removed_user_id = row.user_id
    display_name = None
    if linked_user:
        first_name = decrypt_data(linked_user.first_name) if linked_user.first_name else ''
        last_name = decrypt_data(linked_user.last_name) if linked_user.last_name else ''
        full_name = f'{first_name} {last_name}'.strip()
        display_name = full_name or linked_user.email
        linked_user.organization_id = None
        linked_user.organization_document_number = None
        linked_user.organization_document_number_hash = None
        linked_user.organization_document_img = None

    now = datetime.now()
    peer_rows = (
        db.query(models.ApprovedUsers)
        .filter(
            models.ApprovedUsers.user_id == removed_user_id,
            models.ApprovedUsers.organization_id == row.organization_id,
            models.ApprovedUsers.organization_identification_number == row.organization_identification_number,
        )
        .all()
    )
    for peer in peer_rows:
        peer.access_removed_at = now
        peer.removed_user_id = removed_user_id
        peer.removed_user_display_name = display_name
        peer.user_id = None

    empty_slot = models.ApprovedUsers(
        organization_name=row.organization_name,
        organization_identification_number=row.organization_identification_number,
        duration_in_days=row.duration_in_days,
        subscription_type=row.subscription_type,
        organization_id=row.organization_id,
        user_id=None,
        membership_status='active',
        discount_eligible='1',
    )
    db.add(empty_slot)
    db.flush()

    db.add(models.ApprovedUsersLogs(
        approved_user_id=row.id,
        organization_name=row.organization_name,
        organization_identification_number=row.organization_identification_number,
        duration_in_days=row.duration_in_days,
        organization_id=row.organization_id,
        user_id=actor.id if actor else None,
        message=f'User access removed (previous user_id={removed_user_id})',
        action_type='3',
    ))
    db.add(models.ApprovedUsersLogs(
        approved_user_id=empty_slot.id,
        organization_name=empty_slot.organization_name,
        organization_identification_number=empty_slot.organization_identification_number,
        duration_in_days=empty_slot.duration_in_days,
        organization_id=empty_slot.organization_id,
        user_id=actor.id if actor else None,
        message='Empty slot created after user access was removed',
        action_type='0',
    ))
    db.commit()
    return BaseController.success({"new_record_id": empty_slot.id}, "User access removed.")


@router.post("/pre-approved-users/{record_id}/rejoin", dependencies=[Depends(admin_required)])
def rejoin_approved_user(
    record_id: int,
    payload: admin.RejoinApprovedUser,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
):
    """Rejoin a previously deactivated member."""
    from app.utils.org_membership import create_rejoin, get_active_approved_user

    previous = db.query(models.ApprovedUsers).filter(models.ApprovedUsers.id == record_id).first()
    if not previous:
        return BaseController.errorGeneral("Pre-approved user record not found.", status.HTTP_404_NOT_FOUND)
    if previous.membership_status != 'inactive':
        return BaseController.errorGeneral(
            "Only an inactive record can be rejoined.", status.HTTP_409_CONFLICT,
        )
    if not previous.user_id:
        return BaseController.errorGeneral(
            "This record has no user assigned — access was removed and the user cannot rejoin here.",
            status.HTTP_409_CONFLICT,
        )
    if previous.access_removed_at is not None:
        return BaseController.errorGeneral(
            "User access was removed on this record — the user cannot rejoin here.",
            status.HTTP_409_CONFLICT,
        )
    if previous.organization is not None and previous.organization.is_active != '1':
        return BaseController.errorGeneral(
            "Cannot rejoin: the organization is inactive.",
            status.HTTP_409_CONFLICT,
        )
    if get_active_approved_user(db, user_id=previous.user_id):
        return BaseController.errorGeneral(
            "This user already has an active membership.", status.HTTP_409_CONFLICT,
        )
    if (
        db.query(models.ApprovedUsers.id)
        .filter(
            models.ApprovedUsers.organization_id == previous.organization_id,
            models.ApprovedUsers.organization_identification_number == previous.organization_identification_number,
            models.ApprovedUsers.membership_status == 'active',
        )
        .first()
        is not None
    ):
        return BaseController.errorGeneral(
            "Another active member already exists under this organization identification number.",
            status.HTTP_409_CONFLICT,
        )
    if payload.duration_in_days is None or payload.duration_in_days < 0:
        return BaseController.errorGeneral(
            "duration_in_days must be zero or positive.", status.HTTP_400_BAD_REQUEST,
        )
    subscription_type = payload.subscription_type if payload.subscription_type in ('0', '1', '2', '3') else '0'
    discount_eligible = payload.discount_eligible if payload.discount_eligible in ('0', '1') else '1'

    token_data = verify_access_token(token)
    actor = db.query(models.User).filter(models.User.email == token_data.email).first()
    new_row = create_rejoin(
        db,
        previous=previous,
        duration_in_days=int(payload.duration_in_days),
        subscription_type=subscription_type,
        discount_eligible=discount_eligible,
        actor_id=actor.id if actor else None,
    )
    db.commit()
    return BaseController.success({"new_record_id": new_row.id}, "Member rejoined.")


@router.patch("/pre-approved-users/{record_id}/discount-eligible", dependencies=[Depends(admin_required)])
def set_approved_user_discount_eligible(
    record_id: int,
    payload: admin.SetApprovedUserDiscountEligible,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
):
    """Toggle the discount-eligible flag on a pre-approved-user row."""
    if payload.discount_eligible not in ('0', '1'):
        return BaseController.errorGeneral(
            "discount_eligible must be '0' or '1'.", status.HTTP_400_BAD_REQUEST,
        )
    row = db.query(models.ApprovedUsers).filter(models.ApprovedUsers.id == record_id).first()
    if not row:
        return BaseController.errorGeneral("Pre-approved user record not found.", status.HTTP_404_NOT_FOUND)

    if row.membership_status != 'active':
        return BaseController.errorGeneral(
            "Discount eligibility can only be changed while the membership is active. Rejoin the member first.",
            status.HTTP_409_CONFLICT,
        )

    if row.discount_eligible == payload.discount_eligible:
        return BaseController.success([], "No change.")

    token_data = verify_access_token(token)
    actor = db.query(models.User).filter(models.User.email == token_data.email).first()
    row.discount_eligible = payload.discount_eligible
    db.add(models.ApprovedUsersLogs(
        approved_user_id=row.id,
        organization_name=row.organization_name,
        organization_identification_number=row.organization_identification_number,
        duration_in_days=row.duration_in_days,
        organization_id=row.organization_id,
        user_id=actor.id if actor else None,
        message=(
            "Discount eligibility enabled"
            if payload.discount_eligible == '1'
            else "Discount eligibility disabled"
        ),
        action_type='1',
    ))
    db.commit()
    return BaseController.success([], "Discount eligibility updated.")


@router.put("/update-subscription-options/{user_id}", dependencies=[Depends(admin_required)])
def update_subscription_options(
    user_id: int,
    payload: admin.UpdateSubscriptionProducts,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
):
    db_user = db.query(models.User).filter(models.User.id == user_id).first()
    if not db_user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], 400)

    existing_products = db_user.subscription_products or {}
    products = {}
    for plan in SUBSCRIPTION_PLAN_TYPES:
        plan_payload = getattr(payload, plan, None)
        if plan_payload is not None:
            products[plan] = {slot: plan_payload.model_dump()[slot] for slot in SUBSCRIPTION_SLOTS}
        else:
            products[plan] = existing_products.get(plan) or dict(SUBSCRIPTION_PLAN_DEFAULTS[plan])
    db_user.subscription_products = products

    # Mirror standard slot into the legacy columns so existing read paths (mobile
    # /subscription-options today reads referrer's user.<slot>_subscription_product_id)
    # keep working until they migrate to the JSON column.
    standard = products["standard"]
    db_user.annual_subscription_product_id = standard["annual"]
    db_user.monthly_subscription_product_id = standard["monthly"]
    db_user.ios_annual_subscription_product_id = standard["ios_annual"]
    db_user.ios_monthly_subscription_product_id = standard["ios_monthly"]

    db.commit()
    return BaseController.success([], "Record Updated Successfully")


# ──────────────────────────────────────────────
# Items Management
# ──────────────────────────────────────────────
@router.get("/inactive_items", dependencies=[Depends(admin_required)])
def get_inactive_items(
    db: Session = Depends(get_db),
    search: Optional[str] = Query(default=None, description="Search by item name or supplier name"),
    sort_by: Optional[str] = Query(default='created_at', description="Field to sort by: 'created_at', 'item_name', 'item_price'"),
    sort_order: Optional[str] = Query(default='desc', description="Sort order: 'asc' or 'desc'"),
    selected_language: Optional[str] = Cookie(default='en'),
    limit: int = Query(default=10, ge=1, le=100, description="Number of items to return"),
    page: int = Query(default=0, ge=0, description="Page number starting from 0"),
):
    # Only show items that need admin review — auto-flagged and awaiting action
    items_query = (
        db.query(models.SupplierItem)
        .join(models.Supplier, models.SupplierItem.supplier_id == models.Supplier.id)
        .join(models.User, models.Supplier.user_id == models.User.id)
        .filter(models.SupplierItem.moderation_status.in_([ModerationStatus.PENDING_REVIEW, ModerationStatus.REJECTED]))
        .filter(models.SupplierItem.deleted_at.is_(None))
    )

    if search:
        search_hash = hash_sort_string(search)
        items_query = items_query.filter(
            or_(
                models.SupplierItem.item_name.ilike(f"%{search}%"),
                models.User.first_name_hash == search_hash,
                models.User.last_name_hash == search_hash,
                models.User.email.ilike(f"%{search}%"),
            )
        )

    sort_column_map = {
        'created_at': models.SupplierItem.created_at,
        'item_name': models.SupplierItem.item_name,
        'item_price': models.SupplierItem.item_price,
    }
    sort_col = sort_column_map.get(sort_by, models.SupplierItem.created_at)
    items_query = items_query.order_by(desc(sort_col) if sort_order == 'desc' else asc(sort_col))

    total_items = items_query.count()
    items_list = items_query.offset(page * limit).limit(limit).all()

    result = []
    for item in items_list:
        supplier = item.supplier
        user = supplier.user
        gallery = [
            {
                "id": img.id,
                "image_url": img.image_url,
                "sort_order": img.sort_order,
                "is_flagged": img.is_flagged,
                "flagged_categories": img.flagged_categories,
                "flagged_reason": img.flagged_reason,
            }
            for img in sorted(
                [i for i in (item.images or []) if i.deleted_at is None],
                key=lambda i: (i.sort_order, i.id),
            )
        ]
        result.append({
            "id": item.id,
            "item_name": item.item_name,
            "item_unit": item.item_unit,
            "item_type": item.item_type,
            "item_quantity": item.item_quantity,
            "item_price": f"{round(float(item.item_price), 2):.2f}" if item.item_price is not None else None,
            "currency_code": user.currency.code if user.currency else None,
            "currency_symbol": user.currency.symbol if user.currency else None,
            "item_image": item.item_image,
            "item_images": gallery,
            "status": item.status,
            "moderation_status": int(item.moderation_status) if item.moderation_status is not None else None,
            "rejection_reason": item.rejection_reason,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "supplier_details": {
                "id": supplier.id,
                "status": supplier.status,
                "user_id": user.id,
                "first_name": decrypt_data(user.first_name),
                "last_name": decrypt_data(user.last_name),
                "company_name": decrypt_data(user.company_name),
                "email": user.email,
                "profile_img": user.profile_img,
            },
        })

    return PaginationResponse(
        total=total_items,
        page=page,
        per_page=limit,
        data=result,
        message=messages[selected_language]['items_list'],
    )


@router.put("/items/{item_id}/moderation", dependencies=[Depends(admin_required)])
def moderate_item(
    item_id: int,
    payload: admin.ItemModerationAction,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(admin_required),
    selected_language: Optional[str] = Cookie(default='en'),
):
    if payload.action not in ('approve', 'reject'):
        return BaseController.errorGeneral("action must be 'approve' or 'reject'", status.HTTP_400_BAD_REQUEST)

    db_item = db.query(models.SupplierItem).filter(
        models.SupplierItem.id == item_id,
        models.SupplierItem.deleted_at.is_(None),
    ).first()
    if not db_item:
        return BaseController.errorGeneral(messages[selected_language]['item_not_found'], status.HTTP_404_NOT_FOUND)

    supplier_user = db_item.supplier.user if db_item.supplier else None

    if payload.action == 'approve':
        db_item.status = '1'
        db_item.moderation_status = ModerationStatus.APPROVED
        db_item.rejection_reason = None
        db.add(models.ItemModerationLog(
            supplier_item_id=db_item.id,
            action='admin_approved',
            admin_note=payload.note,
            reviewed_by=current_user.id,
        ))
        db.commit()
        _notify_item_moderation_result(supplier_user, db, 'item_approved_by_admin', 'item-approved-by-admin', None, background_tasks)
        return BaseController.success([], messages[selected_language]['item_updated'])

    # reject
    if not payload.note or not payload.note.strip():
        return BaseController.errorGeneral("A rejection note is required", status.HTTP_400_BAD_REQUEST)

    db_item.status = '0'
    db_item.moderation_status = ModerationStatus.REJECTED
    db_item.rejection_reason = payload.note
    db.add(models.ItemModerationLog(
        supplier_item_id=db_item.id,
        action='admin_rejected',
        admin_note=payload.note,
        reviewed_by=current_user.id,
    ))
    db.commit()
    _notify_item_moderation_result(supplier_user, db, 'item_rejected_by_admin', 'item-rejected-by-admin', payload.note, background_tasks)
    return BaseController.success([], messages[selected_language]['item_updated'])


@router.get("/items/{item_id}/moderation-logs", dependencies=[Depends(admin_required)])
def get_item_moderation_logs(
    item_id: int,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
):
    db_item = db.query(models.SupplierItem).filter(models.SupplierItem.id == item_id).first()
    if not db_item:
        return BaseController.errorGeneral(messages[selected_language]['item_not_found'], status.HTTP_404_NOT_FOUND)

    logs = (
        db.query(models.ItemModerationLog)
        .filter(models.ItemModerationLog.supplier_item_id == item_id)
        .order_by(desc(models.ItemModerationLog.created_at), desc(models.ItemModerationLog.id))
        .all()
    )
    result = [
        {
            "id": log.id,
            "action": log.action,
            "flagged_reason": log.flagged_reason,
            "flagged_categories": log.flagged_categories,
            "openai_response": log.openai_response,
            "admin_note": log.admin_note,
            "reviewed_by": log.reviewed_by,
            "reviewer_name": f"{decrypt_data(log.reviewer.first_name)} {decrypt_data(log.reviewer.last_name)}" if log.reviewer else None,
            "created_at": log.created_at,
        }
        for log in logs
    ]
    return BaseController.success(jsonable_encoder(result), messages[selected_language]['moderation_logs_retrieved'])


@router.delete("/items/{item_id}", dependencies=[Depends(admin_required)])
def delete_item(
    item_id: int,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
):
    db_item = db.query(models.SupplierItem).filter(
        models.SupplierItem.id == item_id,
        models.SupplierItem.deleted_at.is_(None),
    ).first()
    if not db_item:
        return BaseController.errorGeneral(messages[selected_language]['item_not_found'], status.HTTP_404_NOT_FOUND)

    db_item.deleted_at = datetime.now()
    db.commit()
    return BaseController.success([], messages[selected_language]['item_deleted'])


def _notify_item_moderation_result(user, db, notification_template_name, email_slug, rejection_note, background_tasks):
    """Send push + email to the supplier after admin approval/rejection."""
    if not user:
        return

    if user.fcm_token and user.send_push_notification == "0":
        notification_obj = get_notification_data(notification_template_name, user.language_id, db)
        if notification_obj:
            body = notification_obj["body"]
            if rejection_note:
                body = body.replace("[reason]", rejection_note)
            # Mirror the substituted body into the FCM data payload — Android clients
            # read `data.body`, so leaving the raw "[reason]" here shows the literal
            # placeholder on Android even after we swap `body` locally.
            notification_obj["body"] = body
            try:
                send_push_notification(
                    token=user.fcm_token,
                    title=notification_obj["title"],
                    body=body,
                    platform=decrypt_data(user.device_type),
                    data=notification_obj,
                )
            except Exception:
                pass

    email_template = get_email_template_content(db, user.language_id, email_slug)
    if email_template and background_tasks and user.send_mail_notification == "0":
        email_body = email_template.body
        if rejection_note:
            email_body = email_body.replace("[reason]", rejection_note)
        background_tasks.add_task(
            send_moderation_email,
            email=user.email,
            subject=email_template.subject,
            body=email_body,
            selected_language=user.language.code if user.language else 'en',
        )


# ──────────────────────────────────────────────
# Broadcast Audience Configs
# ──────────────────────────────────────────────


def _serialize_broadcast_audience(row: models.BroadcastAudienceConfig) -> dict:
    """Build the admin-facing audience payload including per-language labels
    with language code/name attached for the dashboard picker."""
    payload = admin.BroadcastAudienceConfigOut.model_validate(row).model_dump()
    payload['translations'] = [
        {
            'id': t.id,
            'language_id': t.language_id,
            'label': t.label,
            'description': t.description,
            'language_code': t.language.code if t.language else None,
            'language_name': t.language.name if t.language else None,
        }
        for t in (row.translations or [])
    ]
    return payload


def _apply_broadcast_audience_translations(db: Session, audience: models.BroadcastAudienceConfig, translations):
    """Replace the audience's translation set with ``translations``.

    Semantics:
      * A language present in the incoming list is upserted (label + description
        overwritten if a row exists, inserted otherwise).
      * A language NOT in the incoming list has its existing translation row
        deleted. Pass ``[]`` to strip every translation.

    Duplicate language_ids in the incoming payload use the last-seen values.
    """
    incoming = {t.language_id: (t.label, t.description) for t in translations}
    existing = {t.language_id: t for t in (audience.translations or [])}

    # Remove translations for languages not in the incoming set.
    for lang_id, row in list(existing.items()):
        if lang_id not in incoming:
            db.delete(row)

    # Upsert everything in the incoming set.
    for lang_id, (label, description) in incoming.items():
        if lang_id in existing:
            existing[lang_id].label = label
            existing[lang_id].description = description
        else:
            db.add(models.BroadcastAudienceConfigTranslation(
                audience_config_id=audience.id,
                language_id=lang_id,
                label=label,
                description=description,
            ))


@router.get("/broadcast-audiences", dependencies=[Depends(admin_required)])
def list_broadcast_audiences(
    db: Session = Depends(get_db),
    include_disabled: bool = Query(default=True, description="Include disabled rows"),
):
    """List every non-deleted broadcast audience — presets and admin-created."""
    query = (
        db.query(models.BroadcastAudienceConfig)
        .options(
            joinedload(models.BroadcastAudienceConfig.translations)
            .joinedload(models.BroadcastAudienceConfigTranslation.language),
        )
        .filter(models.BroadcastAudienceConfig.deleted_at.is_(None))
    )
    if not include_disabled:
        query = query.filter(models.BroadcastAudienceConfig.is_enabled.is_(True))
    rows = query.order_by(
        models.BroadcastAudienceConfig.display_order.asc(),
        models.BroadcastAudienceConfig.id.asc(),
    ).all()
    data = [_serialize_broadcast_audience(row) for row in rows]
    return BaseController.success(jsonable_encoder(data), "Audiences fetched successfully.")


@router.get("/broadcast-audiences/{audience_id}", dependencies=[Depends(admin_required)])
def get_broadcast_audience(
    audience_id: int,
    db: Session = Depends(get_db)
):
    row = (
        db.query(models.BroadcastAudienceConfig)
        .options(
            joinedload(models.BroadcastAudienceConfig.translations)
            .joinedload(models.BroadcastAudienceConfigTranslation.language),
        )
        .filter_by(id=audience_id)
        .filter(models.BroadcastAudienceConfig.deleted_at.is_(None))
        .first()
    )
    if not row:
        return BaseController.errorGeneral("Broadcast audience not found.", status.HTTP_404_NOT_FOUND)
    return BaseController.success(jsonable_encoder(_serialize_broadcast_audience(row)), "Broadcast audience fetched successfully.")


@router.post("/broadcast-audiences", dependencies=[Depends(admin_required)])
def create_broadcast_audience(
    payload: admin.BroadcastAudienceConfigCreate,
    db: Session = Depends(get_db)
):
    """Admin builds a custom audience via the dashboard"""
    existing = (
        db.query(models.BroadcastAudienceConfig)
        .filter_by(key=payload.key)
        .filter(models.BroadcastAudienceConfig.deleted_at.is_(None))
        .first()
    )
    if existing:
        return BaseController.errorGeneral(
            "An audience with this key already exists. Please choose a different key.",
            status.HTTP_409_CONFLICT,
        )
    row = models.BroadcastAudienceConfig(
        key=payload.key,
        label=payload.label,
        description=payload.description,
        icon=payload.icon,
        is_preset=False,   # admin-created rows are never presets
        is_enabled=payload.is_enabled,
        min_tier=payload.min_tier,
        filter_definition=payload.filter_definition.model_dump(),
        display_order=payload.display_order,
    )
    db.add(row)
    db.flush()
    if payload.translations:
        _apply_broadcast_audience_translations(db, row, payload.translations)
    db.commit()
    db.refresh(row)
    return BaseController.success(jsonable_encoder(_serialize_broadcast_audience(row)), "Broadcast audience created successfully.")


@router.put("/broadcast-audiences/{audience_id}", dependencies=[Depends(admin_required)])
def update_broadcast_audience(
    audience_id: int,
    payload: admin.BroadcastAudienceConfigUpdate,
    db: Session = Depends(get_db)
):
    """Partial update. Any field left as ``None`` is preserved"""
    row = (
        db.query(models.BroadcastAudienceConfig)
        .options(joinedload(models.BroadcastAudienceConfig.translations))
        .filter_by(id=audience_id)
        .filter(models.BroadcastAudienceConfig.deleted_at.is_(None))
        .first()
    )
    if not row:
        return BaseController.errorGeneral("Broadcast audience not found.", status.HTTP_404_NOT_FOUND)

    if payload.label is not None:
        row.label = payload.label
    if payload.description is not None:
        row.description = payload.description
    if "icon" in payload.model_fields_set:
        if payload.icon is None:
            _remove_broadcast_audience_icon_file(row.icon)
            row.icon = None
        else:
            row.icon = payload.icon
    if payload.is_enabled is not None:
        row.is_enabled = payload.is_enabled
    if payload.min_tier is not None:
        row.min_tier = payload.min_tier
    if payload.filter_definition is not None:
        row.filter_definition = payload.filter_definition.model_dump()
    if payload.display_order is not None:
        row.display_order = payload.display_order
    if payload.translations is not None:
        _apply_broadcast_audience_translations(db, row, payload.translations)

    db.commit()
    db.refresh(row)
    return BaseController.success(jsonable_encoder(_serialize_broadcast_audience(row)), "Broadcast audience updated successfully.")


@router.delete("/broadcast-audiences/{audience_id}", dependencies=[Depends(admin_required)])
def delete_broadcast_audience(
    audience_id: int,
    db: Session = Depends(get_db)
):
    """Soft-delete a custom (admin-created) audience."""
    row = (
        db.query(models.BroadcastAudienceConfig)
        .filter_by(id=audience_id)
        .filter(models.BroadcastAudienceConfig.deleted_at.is_(None))
        .first()
    )
    if not row:
        return BaseController.errorGeneral("Broadcast audience not found.", status.HTTP_404_NOT_FOUND)
    if row.is_preset:
        return BaseController.errorGeneral(
            "Preset audiences cannot be deleted.", status.HTTP_409_CONFLICT,
        )
    row.deleted_at = datetime.now()
    row.key = f"{row.key}__deleted_{row.id}"
    db.commit()
    return BaseController.success([], "Broadcast audience deleted successfully.")


# ──────────────────────────────────────────────
# Broadcast — icon upload
# ──────────────────────────────────────────────
_BROADCAST_ICONS_DIR = "static/icons/broadcast_audiences"


def _remove_broadcast_audience_icon_file(icon_path: Optional[str]) -> None:
    """Best-effort delete of a previously uploaded icon file."""
    if not icon_path:
        return
    normalized = icon_path.lstrip('/')
    if not normalized.startswith(_BROADCAST_ICONS_DIR):
        return
    absolute = os.path.join(settings.FILE_DIR_PATH, normalized)
    try:
        if os.path.isfile(absolute):
            os.remove(absolute)
    except OSError:
        # Missing / permission-denied — don't fail the API call over it.
        pass


@router.post("/broadcast-audiences/icon", dependencies=[Depends(admin_required)])
async def upload_broadcast_audience_icon(
    icon: UploadFile = File(...),
    selected_language: Optional[str] = Cookie(default='en'),
):
    """Standalone icon upload for the broadcast-audience form. Returns the
    stored path so the frontend can POST/PUT the audience with the path
    embedded in the JSON payload."""
    if not icon.content_type or not icon.content_type.startswith("image/"):
        return BaseController.errorGeneral(messages[selected_language]['invalid_file_format'], 400)
    if not allowed_file(icon.filename):
        return BaseController.errorGeneral(messages[selected_language]['invalid_extension'], 400)

    upload_dir = os.path.join(settings.FILE_DIR_PATH, _BROADCAST_ICONS_DIR)
    os.makedirs(upload_dir, exist_ok=True)
    ext = icon.filename.rsplit('.', 1)[-1]
    new_filename = f"aud_{uuid.uuid4()}.{ext}"
    file_path = os.path.join(_BROADCAST_ICONS_DIR, new_filename)
    with open(os.path.join(settings.FILE_DIR_PATH, file_path), "wb") as fh:
        fh.write(await icon.read())
    return BaseController.success({"icon": file_path}, "Icon uploaded successfully.")


# ──────────────────────────────────────────────
# Broadcast — per-tier monthly limits
# ──────────────────────────────────────────────
_BROADCAST_TIER_LIMIT_KEYS = {
    'standard': ('0', 'broadcast_monthly_limit_standard'),
    'gold':     ('1', 'broadcast_monthly_limit_gold'),
    'silver':   ('2', 'broadcast_monthly_limit_silver'),
    'diamond':  ('3', 'broadcast_monthly_limit_diamond'),
}


def _read_broadcast_tier_limit(db: Session, key_name: str):
    """Return the integer limit stored under ``key_name`` in the settings
    table. An empty-string value means UNLIMITED (returns None).
    """
    row = db.query(models.Setting).filter_by(key_name=key_name).first()
    if not row or row.value is None or str(row.value).strip() == '':
        return None  # unlimited
    try:
        return int(row.value)
    except (ValueError, TypeError):
        return None


@router.get("/broadcast-tier-limits", dependencies=[Depends(admin_required)])
def get_broadcast_tier_limits(
    db: Session = Depends(get_db)
):
    """Read the per-tier monthly broadcast caps stored in the settings
    table. Response is a list of one row per tier so the admin dashboard
    can render a small editable table."""
    data = []
    for tier_label, (sub_type, key_name) in _BROADCAST_TIER_LIMIT_KEYS.items():
        data.append({
            "subscription_type": sub_type,
            "tier_label": tier_label,
            "monthly_limit": _read_broadcast_tier_limit(db, key_name),
        })
    return BaseController.success(jsonable_encoder(data), "Broadcast tier limits fetched successfully.")


@router.put("/broadcast-tier-limits", dependencies=[Depends(admin_required)])
def update_broadcast_tier_limits(
    payload: admin.BroadcastTierLimitsUpdate,
    db: Session = Depends(get_db)
):
    """Update per-tier monthly caps. Any tier field omitted from the
    payload is left untouched. Send ``null`` for a tier's ``monthly_limit``
    to mark it unlimited; ``0`` to block it entirely.
    """
    incoming = payload.model_dump(exclude_unset=True)
    for tier_label, limit in incoming.items():
        if tier_label not in _BROADCAST_TIER_LIMIT_KEYS:
            continue
        _, key_name = _BROADCAST_TIER_LIMIT_KEYS[tier_label]
        stored_value = '' if limit is None else str(limit)
        row = db.query(models.Setting).filter_by(key_name=key_name).first()
        if row:
            row.value = stored_value
        else:
            db.add(models.Setting(
                key_name=key_name,
                key=key_name,
                value=stored_value,
                value_type='integer',
                description=f"Max broadcast campaigns per calendar month for {tier_label} suppliers. Empty string = unlimited; 0 = blocked.",
            ))
    db.commit()

    # Return the fresh state so the dashboard reflects the update.
    data = []
    for tier_label, (sub_type, key_name) in _BROADCAST_TIER_LIMIT_KEYS.items():
        data.append({
            "subscription_type": sub_type,
            "tier_label": tier_label,
            "monthly_limit": _read_broadcast_tier_limit(db, key_name),
        })
    return BaseController.success(jsonable_encoder(data), "Broadcast tier limits updated successfully.")


# ──────────────────────────────────────────────
# Broadcast Moderation
# ──────────────────────────────────────────────

@router.get("/broadcasts", dependencies=[Depends(admin_required)])
def list_broadcasts(
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
    supplier_name: Optional[str] = Query(
        default=None,
        description=(
            "Fuzzy supplier-name search."
        ),
    ),
    audience_label: Optional[str] = Query(
        default=None,
        description=(
            "Case-insensitive substring match against the canonical audience"
        ),
    ),
    date_from: Optional[str] = Query(default=None, description="ISO date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(default=None, description="ISO date (YYYY-MM-DD)"),
    page: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=200),
):
    """Paginated list of every broadcast, newest first. Filterable by
    supplier name / audience label / date range."""
    query = (
        db.query(models.BroadcastCampaign)
        .options(
            joinedload(models.BroadcastCampaign.audience_config),
            joinedload(models.BroadcastCampaign.supplier)
            .joinedload(models.Supplier.user),
        )
    )
    if supplier_name:
        terms = supplier_name.split()
        combos = []
        for i in range(1, len(terms)):
            first_hash = hash_sort_string(" ".join(terms[:i]))
            last_hash = hash_sort_string(" ".join(terms[i:]))
            combos.append(f"{first_hash} {last_hash}")
        query = query.join(
            models.Supplier, models.Supplier.id == models.BroadcastCampaign.supplier_id,
        ).join(
            models.User, models.User.id == models.Supplier.user_id,
        )
        for term in terms:
            term_hash = hash_sort_string(term)
            query = query.filter(
                or_(
                    models.User.first_name_hash == term_hash,
                    models.User.last_name_hash == term_hash,
                    func.concat(
                        models.User.first_name_hash,
                        literal(" "),
                        models.User.last_name_hash,
                    ).in_(combos) if combos else literal(False),
                )
            )
    if audience_label:
        needle = f"%{audience_label}%"
        translation_match = (
            db.query(models.BroadcastAudienceConfigTranslation.audience_config_id)
            .filter(models.BroadcastAudienceConfigTranslation.label.ilike(needle))
        )
        query = query.outerjoin(
            models.BroadcastAudienceConfig,
            models.BroadcastAudienceConfig.id == models.BroadcastCampaign.audience_config_id,
        ).filter(
            or_(
                models.BroadcastAudienceConfig.label.ilike(needle),
                models.BroadcastCampaign.audience_config_id.in_(translation_match),
            )
        )
    if date_from:
        try:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d")
            query = query.filter(models.BroadcastCampaign.sent_at >= dt_from)
        except ValueError:
            pass
    if date_to:
        try:
            # inclusive: match anything sent up to end-of-day
            dt_to = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            query = query.filter(models.BroadcastCampaign.sent_at < dt_to)
        except ValueError:
            pass

    total = query.with_entities(func.count(models.BroadcastCampaign.id)).scalar() or 0
    rows = (
        query
        .order_by(
            desc(models.BroadcastCampaign.sent_at),
            desc(models.BroadcastCampaign.id),
        )
        .offset(page * limit)
        .limit(limit)
        .all()
    )

    data = []
    for row in rows:
        supplier_name = None
        supplier_company_name = None
        supplier_email = None
        if row.supplier and row.supplier.user:
            first = decrypt_data(row.supplier.user.first_name) if row.supplier.user.first_name else None
            last = decrypt_data(row.supplier.user.last_name) if row.supplier.user.last_name else None
            supplier_name = " ".join(x for x in (first, last) if x) or None
            supplier_company_name = decrypt_data(row.supplier.user.company_name)
            supplier_email = row.supplier.user.email
        data.append({
            "id": row.id,
            "supplier_id": row.supplier_id,
            "supplier_name": supplier_name,
            "supplier_company_name": supplier_company_name,
            "supplier_email": supplier_email,
            "audience_key": row.audience_key,
            "audience_label": row.audience_config.label if row.audience_config else None,
            "message_text": row.message_text,
            "image_url": row.image_url,
            "total_recipients": row.total_recipients,
            "sent_at": row.sent_at,
        })

    return BaseController.success(
        jsonable_encoder({
            "total": total,
            "page": page,
            "per_page": limit,
            "data": data,
        }),
        messages[selected_language]["broadcasts_fetched"],
    )


@router.get("/broadcasts/{campaign_id}", dependencies=[Depends(admin_required)])
def get_broadcast_detail(
    campaign_id: int,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
    page: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500,
                       description="Recipient page size (default 50)"),
):
    """Drill-down for a single broadcast: full campaign + per-recipient
    delivery rows (paginated).
    """
    campaign = (
        db.query(models.BroadcastCampaign)
        .options(
            joinedload(models.BroadcastCampaign.audience_config),
            joinedload(models.BroadcastCampaign.supplier)
            .joinedload(models.Supplier.user),
        )
        .filter(models.BroadcastCampaign.id == campaign_id)
        .first()
    )
    if not campaign:
        return BaseController.errorGeneral(
            "Broadcast not found.", status.HTTP_404_NOT_FOUND,
        )

    recipients_base = (
        db.query(models.BroadcastRecipient)
        .filter(models.BroadcastRecipient.campaign_id == campaign_id)
        .options(joinedload(models.BroadcastRecipient.customer))
    )
    total_recipients = recipients_base.with_entities(
        func.count(models.BroadcastRecipient.id)
    ).scalar() or 0

    recipient_rows = (
        recipients_base
        .order_by(models.BroadcastRecipient.id.asc())
        .offset(page * limit)
        .limit(limit)
        .all()
    )

    recipients = []
    for r in recipient_rows:
        first = decrypt_data(r.customer.first_name) if r.customer and r.customer.first_name else None
        last = decrypt_data(r.customer.last_name) if r.customer and r.customer.last_name else None
        recipients.append({
            "customer_id": r.customer_id,
            "customer_name": " ".join(x for x in (first, last) if x) or None,
            "customer_email": r.customer.email if r.customer else None,
            "message_id": r.message_id,
            "delivered_at": r.delivered_at,
            "removed_before_send": r.removed_before_send,
        })

    supplier_name = None
    supplier_company_name = None
    supplier_email = None
    if campaign.supplier and campaign.supplier.user:
        first = decrypt_data(campaign.supplier.user.first_name) if campaign.supplier.user.first_name else None
        last = decrypt_data(campaign.supplier.user.last_name) if campaign.supplier.user.last_name else None
        supplier_name = " ".join(x for x in (first, last) if x) or None
        supplier_company_name = decrypt_data(campaign.supplier.user.company_name)
        supplier_email = campaign.supplier.user.email

    detail = {
        "id": campaign.id,
        "supplier_id": campaign.supplier_id,
        "supplier_name": supplier_name,
        "supplier_company_name": supplier_company_name,
        "supplier_email": supplier_email,
        "audience_key": campaign.audience_key,
        "audience_label": campaign.audience_config.label if campaign.audience_config else None,
        "audience_snapshot": campaign.audience_snapshot,
        "message_text": campaign.message_text,
        "image_url": campaign.image_url,
        "total_recipients": campaign.total_recipients,
        "sent_at": campaign.sent_at,
        "recipients": {
            "total": total_recipients,
            "page": page,
            "per_page": limit,
            "data": recipients,
        },
    }
    return BaseController.success(
        jsonable_encoder(detail),
        "Broadcast fetched successfully.",
    )


# ──────────────────────────────────────────────
# Additional expenses (admin read-only view)
# ──────────────────────────────────────────────
@router.get("/additional-expenses/{supplier_id}", dependencies=[Depends(admin_required)])
def admin_list_supplier_additional_expenses(
    supplier_id: int,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
    from_date: Optional[str] = Query(default=None, description="expense_date >= YYYY-MM-DD"),
    to_date: Optional[str] = Query(default=None, description="expense_date <= YYYY-MM-DD"),
    expense_type: Optional[str] = Query(default=None, description="0 = personal, 1 = business, 2 = non_identified"),
    sort_by: Optional[str] = Query(default="expense_date", description="'expense_date', 'amount', 'created_at'"),
    sort_order: Optional[str] = Query(default="desc", description="'asc' or 'desc'"),
    page: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=200),
):
    """Read-only paginated view of one supplier's additional-expense ledger."""
    supplier = db.query(models.Supplier).filter_by(id=supplier_id).first()
    if not supplier:
        return BaseController.errorGeneral("Supplier not found.", status.HTTP_404_NOT_FOUND)

    query = db.query(models.AdditionalExpense).filter(
        models.AdditionalExpense.supplier_id == supplier_id
    )
    if from_date:
        query = query.filter(
            cast(models.AdditionalExpense.expense_date, Date) >= convert_date(from_date)
        )
    if to_date:
        query = query.filter(
            cast(models.AdditionalExpense.expense_date, Date) <= convert_date(to_date)
        )
    if expense_type is not None and str(expense_type).strip() != '':
        query = query.filter(
            models.AdditionalExpense.expense_type == str(expense_type).strip()
        )

    total = query.count()

    sort_columns = {
        "expense_date": models.AdditionalExpense.expense_date,
        "amount": models.AdditionalExpense.amount,
        "created_at": models.AdditionalExpense.created_at,
    }
    column = sort_columns.get(sort_by or 'expense_date', models.AdditionalExpense.expense_date)
    query = query.order_by(asc(column) if (sort_order or 'desc').lower() == 'asc' else desc(column))

    rows = query.offset(page * limit).limit(limit).all()
    data = [
        {
            "id": r.id,
            "supplier_id": r.supplier_id,
            "description": r.description,
            "expense_date": r.expense_date,
            "amount": f"{r.amount:.2f}" if r.amount is not None else "0.00",
            "currency_code": r.currency_code,
            "currency_symbol": r.currency_symbol,
            "expense_type": r.expense_type or "2",
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]

    return BaseController.success(
        jsonable_encoder({
            "total": total,
            "page": page,
            "per_page": limit,
            "data": data,
        }),
        "Additional expenses fetched successfully.",
    )


# ─────────────────────────────────────────────────────────────────────
# AI usage / cost reporting
# ─────────────────────────────────────────────────────────────────────
def _parse_iso_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


@router.get("/ai-usage", dependencies=[Depends(admin_required)])
def get_ai_usage_rollup(
    db: Session = Depends(get_db),
    feature: Optional[str] = Query(default=None, description="Filter by feature tag"),
    model: Optional[str] = Query(default=None, description="Filter by model id"),
    from_date: Optional[str] = Query(default=None, description="UTC date YYYY-MM-DD — frontend converts the picker's local date before sending"),
    to_date: Optional[str] = Query(default=None, description="UTC date YYYY-MM-DD — inclusive; server treats as < next UTC day"),
):
    """Aggregate AI usage grouped by feature × month."""
    month_expr = func.date_format(models.AIUsageLog.created_at, "%Y-%m").label("month")
    query = (
        db.query(
            models.AIUsageLog.feature,
            month_expr,
            func.count(models.AIUsageLog.id).label("request_count"),
            func.sum(case((models.AIUsageLog.success.is_(False), 1), else_=0)).label("error_count"),
            func.coalesce(func.sum(models.AIUsageLog.cost_cents), 0).label("spend_cents"),
            func.coalesce(func.avg(models.AIUsageLog.latency_ms), 0).label("avg_latency_ms"),
            func.coalesce(func.sum(models.AIUsageLog.total_tokens), 0).label("total_tokens"),
        )
        .group_by(models.AIUsageLog.feature, month_expr)
        .order_by(month_expr.desc(), models.AIUsageLog.feature)
    )
    if feature:
        query = query.filter(models.AIUsageLog.feature == feature)
    if model:
        query = query.filter(models.AIUsageLog.model == model)
    parsed_from = _parse_iso_date(from_date)
    parsed_to = _parse_iso_date(to_date)
    if parsed_from:
        query = query.filter(models.AIUsageLog.created_at >= parsed_from)
    if parsed_to:
        query = query.filter(models.AIUsageLog.created_at < parsed_to + timedelta(days=1))

    rows = query.all()
    data = [
        {
            "feature": r.feature,
            "month": r.month,
            "request_count": int(r.request_count or 0),
            "error_count": int(r.error_count or 0),
            "spend_cents": float(r.spend_cents or 0),
            "avg_latency_ms": int(r.avg_latency_ms or 0),
            "total_tokens": int(r.total_tokens or 0),
        }
        for r in rows
    ]
    return BaseController.success(jsonable_encoder(data), "AI usage fetched successfully.")


@router.get("/ai-usage/logs", dependencies=[Depends(admin_required)])
def get_ai_usage_logs(
    db: Session = Depends(get_db),
    feature: Optional[str] = Query(default=None, description="Filter by feature tag"),
    model: Optional[str] = Query(default=None, description="Filter by model id"),
    success: Optional[bool] = Query(default=None, description="true / false to filter by outcome"),
    from_date: Optional[str] = Query(default=None, description="UTC date YYYY-MM-DD — frontend converts the picker's local date before sending"),
    to_date: Optional[str] = Query(default=None, description="UTC date YYYY-MM-DD — inclusive; server treats as < next UTC day"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=25, ge=1, le=200),
):
    """Paginated raw ``ai_usage_log`` rows — for debugging a specific spike."""
    query = db.query(models.AIUsageLog)
    if feature:
        query = query.filter(models.AIUsageLog.feature == feature)
    if model:
        query = query.filter(models.AIUsageLog.model == model)
    if success is not None:
        query = query.filter(models.AIUsageLog.success == success)
    parsed_from = _parse_iso_date(from_date)
    parsed_to = _parse_iso_date(to_date)
    if parsed_from:
        query = query.filter(models.AIUsageLog.created_at >= parsed_from)
    if parsed_to:
        query = query.filter(models.AIUsageLog.created_at < parsed_to + timedelta(days=1))

    total = query.count()
    skip = (page - 1) * limit
    rows = (
        query.order_by(desc(models.AIUsageLog.created_at))
        .offset(skip).limit(limit).all()
    )

    user_ids = {r.user_id for r in rows if r.user_id is not None}
    user_name_map: dict[int, str] = {}
    if user_ids:
        for u in db.query(models.User).filter(models.User.id.in_(user_ids)).all():
            first = decrypt_data(u.first_name) if u.first_name else ""
            last = decrypt_data(u.last_name) if u.last_name else ""
            full = f"{first} {last}".strip()
            user_name_map[u.id] = full or (u.email or "")

    data = [
        {
            "id": r.id,
            "feature": r.feature,
            "provider": r.provider,
            "model": r.model,
            "operation": r.operation,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "total_tokens": r.total_tokens,
            "cost_cents": float(r.cost_cents) if r.cost_cents is not None else 0.0,
            "latency_ms": r.latency_ms,
            "success": bool(r.success),
            "error_class": r.error_class,
            "request_id": r.request_id,
            "user_id": r.user_id,
            "user_name": user_name_map.get(r.user_id) if r.user_id else None,
            "context": r.context,
            "created_at": r.created_at,
        }
        for r in rows
    ]
    return BaseController.success(
        jsonable_encoder({
            "total": total,
            "page": page,
            "per_page": limit,
            "data": data,
        }),
        "AI usage logs fetched successfully.",
    )


# ─────────────────────────────────────────────────────────────────────
# Order audit trail (readable by admin)
# ─────────────────────────────────────────────────────────────────────
@router.get("/bookings/{booking_id}/audit", dependencies=[Depends(admin_required)])
def get_booking_audit(
    booking_id: int,
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
):
    """Return the booking_audit_log rows for one order"""
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id).first()
    if not booking:
        return BaseController.errorGeneral(
            messages[selected_language]['booking_not_found'], status.HTTP_404_NOT_FOUND,
        )

    rows = (
        db.query(models.BookingAuditLog)
        .filter(models.BookingAuditLog.booking_id == booking_id)
        .order_by(desc(models.BookingAuditLog.created_at), desc(models.BookingAuditLog.id))
        .all()
    )

    actor_ids = {r.actor_id for r in rows if r.actor_id is not None}
    actor_name_map: dict[int, str] = {}
    if actor_ids:
        for u in db.query(models.User).filter(models.User.id.in_(actor_ids)).all():
            first = decrypt_data(u.first_name) if u.first_name else ""
            last = decrypt_data(u.last_name) if u.last_name else ""
            full = f"{first} {last}".strip()
            actor_name_map[u.id] = full or (u.email or "")

    data = [
        {
            "id": r.id,
            "booking_id": r.booking_id,
            "booking_item_id": r.booking_item_id,
            "actor_id": r.actor_id,
            "actor_name": actor_name_map.get(r.actor_id) if r.actor_id else None,
            "event": r.event,
            "payload": r.payload,
            "created_at": r.created_at,
        }
        for r in rows
    ]
    return BaseController.success(jsonable_encoder(data), "Audit trail fetched successfully.")


@router.get("/bookings/{booking_id}/refund-preview", dependencies=[Depends(admin_required)])
def get_refund_preview(
    booking_id: int,
    amount: Optional[float] = Query(default=None, description="Partial refund amount; omit for full-refund preview"),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
):
    """Live math for the admin dispute-action screen."""
    from decimal import Decimal
    from app.routes.bookings import compute_release_amounts, _decimal

    booking = db.query(models.Booking).filter(models.Booking.id == booking_id).first()
    if not booking:
        return BaseController.errorGeneral(
            messages[selected_language]['booking_not_found'], status.HTTP_404_NOT_FOUND,
        )
    txn = (
        db.query(models.BookingTransaction)
        .filter(models.BookingTransaction.booking_id == booking_id)
        .first()
    )
    if not txn or not txn.is_sct:
        return BaseController.errorGeneral(
            "Refund preview applies only to SCT orders.", status.HTTP_400_BAD_REQUEST,
        )

    class _Simulated:
        pass

    remaining_before = _decimal(txn.amount) - _decimal(txn.refunded_amount)
    stripe_fee = _decimal(txn.stripe_fee_amount)
    # Portion of the order that can be refunded without eating into the
    # Stripe processing fee. Anything beyond this must be issued as a Full
    # Refund (which waives participation and refunds the full balance).
    max_partial_refund = (remaining_before - stripe_fee).quantize(Decimal("0.01"))
    if max_partial_refund < 0:
        max_partial_refund = Decimal("0")

    if amount is None:
        # Full refund → nothing left, no fee, no transfer.
        preview_refund = remaining_before
        fee = Decimal("0")
        net = Decimal("0")
    else:
        partial = Decimal(str(amount))
        if partial <= 0 or partial > remaining_before:
            return BaseController.errorGeneral(
                "Refund amount must be positive and no greater than the remaining balance.",
                status.HTTP_400_BAD_REQUEST,
            )
        if partial > max_partial_refund:
            return BaseController.errorGeneral(
                (
                    "Partial refund cannot cut into the Stripe processing fee. "
                    "Use Full Refund instead, or reduce the refund to no more "
                    f"than {float(max_partial_refund)}."
                ),
                status.HTTP_400_BAD_REQUEST,
            )
        # Simulate post-refund transaction to compute the release split.
        sim = _Simulated()
        sim.amount = txn.amount
        sim.refunded_amount = _decimal(txn.refunded_amount) + partial
        sim.stripe_fee_amount = txn.stripe_fee_amount
        fee, net = compute_release_amounts(db, sim)
        preview_refund = partial

    data = {
        "booking_id": booking_id,
        "amount_total": float(_decimal(txn.amount)),
        "amount_already_refunded": float(_decimal(txn.refunded_amount)),
        "refund_now": float(preview_refund),
        "participation_fee": float(fee),
        "stripe_fee_amount": float(stripe_fee),
        "net_to_supplier": float(net),
        "max_partial_refund": float(max_partial_refund),
    }
    return BaseController.success(jsonable_encoder(data), "Refund preview computed.")


@router.get("/held-funds", dependencies=[Depends(admin_required)])
def get_held_funds(
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
    include_blocked: bool = Query(default=True, description="Include transfer_status='blocked' rows"),
    search: Optional[str] = Query(default=None, description="Search by order number"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=25, ge=1, le=200),
):
    """List of SCT transactions with funds still sitting in the MEIAPPLI
    balance — i.e. captured but not yet released."""
    statuses = ['held']
    if include_blocked:
        statuses.append('blocked')

    q = (
        db.query(models.BookingTransaction)
        .join(models.Booking, models.Booking.id == models.BookingTransaction.booking_id)
        .options(joinedload(models.BookingTransaction.booking))
        .filter(
            models.BookingTransaction.is_sct.is_(True),
            models.BookingTransaction.transfer_status.in_(statuses),
        )
    )
    if search:
        q = q.filter(models.Booking.order_number.ilike(f"%{search.strip()}%"))
    total = q.count()
    skip = (page - 1) * limit
    rows = (
        q.order_by(
            models.BookingTransaction.review_window_ends_at.is_(None).asc(),
            models.BookingTransaction.review_window_ends_at.asc(),
            models.BookingTransaction.id.desc(),
        )
        .offset(skip).limit(limit).all()
    )
    data = []
    for txn in rows:
        booking = txn.booking
        data.append({
            "booking_id": txn.booking_id,
            "order_number": booking.order_number if booking else None,
            "customer_name": booking.customer_name if booking else None,
            "customer_company_name": decrypt_data(booking.user1.company_name) if booking and booking.user1 else None,
            "supplier_name": booking.supplier_name if booking else None,
            "supplier_company_name": decrypt_data(booking.supplier.user.company_name) if booking and booking.supplier and booking.supplier.user else None,
            "amount": float(txn.amount) if txn.amount is not None else 0.0,
            "refunded_amount": float(txn.refunded_amount) if txn.refunded_amount is not None else 0.0,
            "currency_code": booking.currency_code if booking else None,
            "transfer_status": txn.transfer_status,
            "review_window_ends_at": txn.review_window_ends_at,
            "captured_at": txn.captured_at,
            "booking_status": booking.status if booking else None,
        })
    return BaseController.success(
        jsonable_encoder({
            "total": total,
            "page": page,
            "per_page": limit,
            "data": data,
        }),
        "Held funds fetched successfully.",
    )

def _plan_tier_from_product_id(product_id: str) -> str:
    pid = (product_id or "").lower()
    if "diamond" in pid:
        return "Diamond"
    if "gold" in pid:
        return "Gold"
    if "silver" in pid:
        return "Silver"
    return "Standard"


def _plan_period_from_product_id(product_id: str) -> str:
    pid = (product_id or "").lower()
    if "annual" in pid or "annually" in pid:
        return "Annual"
    if "monthly" in pid:
        return "Monthly"
    return "—"


def _trial_label_from_days(duration_days: Optional[int]) -> str:
    return {7: "1 Week", 14: "2 Weeks", 30: "1 Month"}.get(duration_days or 0, f"{duration_days} days" if duration_days else "—")


@router.get("/apple-offer-codes", dependencies=[Depends(admin_required)])
def list_apple_offer_codes(
    product_id: Optional[str] = Query(None, description="Filter by productId"),
    search: Optional[str] = Query(None, description="Search by offer code or productId (case-insensitive)"),
    active_only: bool = Query(False, description="Return only rows with active=1"),
    page: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    q = db.query(models.AppleOfferCode).order_by(models.AppleOfferCode.id.desc())
    if product_id:
        q = q.filter(models.AppleOfferCode.product_id == product_id)
    if search:
        search_value = search.strip()
        if search_value:
            like_pattern = f"%{search_value}%"
            q = q.filter(
                or_(
                    models.AppleOfferCode.code.ilike(like_pattern),
                    models.AppleOfferCode.product_id.ilike(like_pattern),
                )
            )
    if active_only:
        q = q.filter(models.AppleOfferCode.active == 1)
    total = q.count()
    rows = q.offset(page * limit).limit(limit).all()
    data = [
        {
            "id": r.id,
            "product_id": r.product_id,
            "plan_tier": _plan_tier_from_product_id(r.product_id),
            "plan_period": _plan_period_from_product_id(r.product_id),
            "trial_label": _trial_label_from_days(r.duration_days),
            "campaign_id": r.campaign_id,
            "custom_code_id": r.custom_code_id,
            "duration_days": r.duration_days,
            "code": r.code,
            "total_codes": r.total_codes,
            "served_count": r.served_count,
            "confirmed_count": r.confirmed_count or 0,
            "codes_left": max((r.total_codes or 0) - (r.confirmed_count or 0), 0),
            "expiration_date": r.expiration_date.isoformat() if r.expiration_date else None,
            "active": bool(r.active),
            "created_at": r.created_at,
            "deactivated_at": r.deactivated_at,
        }
        for r in rows
    ]
    return BaseController.success(
        jsonable_encoder({"total": total, "page": page, "per_page": limit, "data": data}),
        "Apple offer codes fetched.",
    )


@router.post("/apple-offer-codes/{code_id}/deactivate", dependencies=[Depends(admin_required)])
def deactivate_apple_offer_code(
    code_id: int,
    db: Session = Depends(get_db),
):
    row = db.query(models.AppleOfferCode).filter(models.AppleOfferCode.id == code_id).first()
    if not row:
        return BaseController.errorGeneral("Offer code not found.", status.HTTP_404_NOT_FOUND)
    if not row.active:
        return BaseController.errorGeneral("Offer code is already inactive.", status.HTTP_409_CONFLICT)

    from app.utils.helper import deactivate_apple_custom_code
    ok = deactivate_apple_custom_code(row.custom_code_id)
    if not ok:
        return BaseController.errorGeneral(
            "Failed to deactivate the code on Apple.",
            status.HTTP_502_BAD_GATEWAY,
        )
    row.active = 0
    row.deactivated_at = datetime.now()
    db.commit()
    return BaseController.success(
        jsonable_encoder({
            "id": row.id,
            "active": bool(row.active),
            "deactivated_at": row.deactivated_at,
        }),
        "Offer code deactivated.",
    )
