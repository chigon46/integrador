from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    odoo_url: str
    odoo_db: str
    odoo_user: str
    odoo_password: str
    odoo_lang: str = "es_UY"
    odoo_brand_field: str = "product_brand_id"

    wc_url: str
    wc_consumer_key: str
    wc_consumer_secret: str
    wc_brand_sync_mode: str = "brand"
    wc_brand_endpoint: str = "products/brands"
    wc_brand_attribute_name: str = "Marca"

    webhook_secret: str

    bfl_api_key: str = ""
    image_generation_logo_url: str = (
        "https://sovai.com.uy/wp-content/uploads/2026/03/"
        "cropped-Logo-SOVAi-1-159x37.png"
    )
    image_generation_storage_path: str = "/app/generated_images"
    image_generation_internal_base_url: str = "http://integrator.local:8088"

    bcu_wsdl_url: str = "https://cotizaciones.bcu.gub.uy/wscotizaciones/servlet/awsbcucotizaciones"
    bcu_default_group: int = 1

    enable_background_sync: bool = False
    background_sync_interval_seconds: int = 60
    background_orders_limit: int = 50
    background_products_limit: int = 500
    background_sync_orders: bool = True
    background_sync_inventory: bool = True

    ml_enabled: bool = False
    ml_api_url: str = "https://api.mercadolibre.com"
    ml_seller_id: str = ""
    ml_access_token: str = ""
    ml_refresh_token: str = ""
    ml_client_id: str = ""
    ml_client_secret: str = ""
    ml_rate_limit_per_second: int = 2
    ml_circuit_breaker_threshold: int = 5
    ml_circuit_breaker_cooldown_seconds: int = 60

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
