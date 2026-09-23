# Integrador Odoo - WooCommerce

## Documentacion recomendada

- Guia operativa completa: `DOCUMENTACION_OPERATIVA_SOVAI.md`

## Nota de build en este entorno

El build de la imagen se realiza manualmente:

```bash
docker build -t odoo-woocommerce-integrator .
```

Luego `docker-compose.yaml` levanta el servicio apuntando a esa imagen:

```bash
docker compose up -d
```

Este servicio conecta Odoo con WooCommerce para sincronizar catalogo de productos y preparar el flujo de pedidos entre la tienda web y el ERP.

## Objetivo de esta primera etapa

La primera etapa implementada sincroniza productos desde Odoo hacia WooCommerce.

El criterio definido es:

- Odoo es el sistema maestro de productos.
- WooCommerce es la tienda web y canal de venta.
- El SKU une el producto de Odoo con el producto de WooCommerce.
- El integrador crea o actualiza productos en WooCommerce usando datos obtenidos desde Odoo.

## Arquitectura

Los tres servicios se comunican por una red Docker interna compartida:

```text
Odoo 18  --->  Integrador FastAPI  --->  WordPress / WooCommerce
```

La red interna usada es:

```text
integration_net
```

Por eso el integrador no necesita usar IP publica ni puertos externos para hablar con Odoo o WordPress.

Variables principales:

```env
ODOO_URL=http://odoo:8069
WC_URL=http://wordpress
```

## Componentes

El integrador esta hecho en Python con FastAPI.

Archivos principales:

- `app/main.py`: endpoints HTTP del integrador.
- `app/odoo_client.py`: conexion XML-RPC con Odoo.
- `app/woocommerce_client.py`: conexion REST con WooCommerce.
- `app/settings.py`: lectura de variables de entorno.
- `docker-compose.yaml`: definicion del contenedor.
- `Dockerfile`: imagen del servicio.
- `.env.example`: ejemplo de configuracion.

## Configuracion requerida

El archivo `.env` debe contener:

```env
ODOO_URL=http://odoo:8069
ODOO_DB=nombre_de_la_base_odoo
ODOO_USER=usuario_odoo
ODOO_PASSWORD=password_odoo
ODOO_LANG=es_UY
ODOO_BRAND_FIELD=product_brand_id

WC_URL=http://wordpress
WC_CONSUMER_KEY=ck_xxx
WC_CONSUMER_SECRET=cs_xxx
WC_BRAND_SYNC_MODE=brand
WC_BRAND_ENDPOINT=products/brands
WC_BRAND_ATTRIBUTE_NAME=Marca

WEBHOOK_SECRET=clave_larga_para_webhooks

ENABLE_BACKGROUND_SYNC=false
BACKGROUND_SYNC_INTERVAL_SECONDS=60
BACKGROUND_ORDERS_LIMIT=50
BACKGROUND_PRODUCTS_LIMIT=500
BACKGROUND_SYNC_ORDERS=true
BACKGROUND_SYNC_INVENTORY=true
```

La API key de WooCommerce se crea en:

```text
WooCommerce -> Ajustes -> Avanzado -> API REST
```

Debe tener permisos:

```text
Lectura/Escritura
```

## Sincronizacion de productos

Endpoint:

```http
POST /sync/products?limit=5
```

Ejemplo desde el servidor:

```bash
curl -X POST 'http://localhost:8088/sync/products?limit=5'
```

Resultado de prueba obtenido:

```json
{
  "count": 5,
  "products": [
    {"sku": "E-COM06", "woocommerce_id": 145},
    {"sku": "E-COM07", "woocommerce_id": 146},
    {"sku": "E-COM08", "woocommerce_id": 147},
    {"sku": "E-COM09", "woocommerce_id": 148},
    {"sku": "E-COM10", "woocommerce_id": 149}
  ]
}
```

Esto confirma que el integrador pudo:

- Conectarse a Odoo.
- Leer productos vendibles con SKU.
- Conectarse a WooCommerce.
- Crear o actualizar productos en WooCommerce.

## Reconciliacion de pedidos

Para evitar perder ventas si Odoo o el integrador estan caidos, el integrador incluye una sincronizacion manual de pedidos.

Endpoint:

```http
POST /sync/orders?limit=50
```

Ejemplo desde el servidor:

```bash
curl -X POST 'http://localhost:8088/sync/orders?limit=50'
```

Este proceso:

- Busca pedidos de WooCommerce en estado `processing` o `completed`.
- Intenta crear cada pedido como `sale.order` en Odoo.
- Antes de crear, verifica si ya existe una orden en Odoo con referencia `WooCommerce #ID`.
- Marca el pedido en WooCommerce con metadata de sincronizacion.

Metadata escrita en WooCommerce cuando sincroniza correctamente:

```text
_odoo_sale_order_id = ID de sale.order en Odoo
_odoo_sync_status = synced
```

Metadata escrita si hay error:

```text
_odoo_sync_status = sync_error
_odoo_sync_error = mensaje del error
```

Esto permite reintentar la sincronizacion sin duplicar pedidos en Odoo.

## Sincronizacion automatica

El integrador puede trabajar sin depender del webhook de WooCommerce.

Para activarlo, configurar en `.env`:

```env
ENABLE_BACKGROUND_SYNC=true
BACKGROUND_SYNC_INTERVAL_SECONDS=60
BACKGROUND_ORDERS_LIMIT=50
BACKGROUND_PRODUCTS_LIMIT=500
BACKGROUND_SYNC_ORDERS=true
BACKGROUND_SYNC_INVENTORY=true
```

Con esa configuracion, cada 60 segundos el integrador ejecuta:

```text
WooCommerce -> Odoo: pedidos processing/completed
Odoo -> WooCommerce: precio y stock
```

La sincronizacion automatica de inventario usa el modo seguro: no pisa nombre, descripcion ni imagenes en WooCommerce.

El webhook puede quedar como mecanismo opcional. La reconciliacion por intervalo queda como mecanismo principal para no perder pedidos si Odoo o el integrador estuvieron caidos.

## Campos sincronizados

Desde Odoo se leen productos del modelo:

```text
product.product
```

Filtro actual:

```text
sale_ok = true
default_code != false
```

Campos usados:

- `name`: nombre del producto.
- `default_code`: SKU.
- `list_price`: precio de venta.
- `qty_available`: stock disponible.
- `description_sale`: descripcion comercial.
- campo de marca configurable con `ODOO_BRAND_FIELD`, por defecto `product_brand_id`.

En WooCommerce se crean o actualizan productos simples con:

- Nombre.
- SKU.
- Precio regular.
- Stock gestionado.
- Cantidad de stock.
- Descripcion.
- Marca real de WooCommerce si `WC_BRAND_SYNC_MODE=brand`.
- Atributo de marca si `WC_BRAND_SYNC_MODE=attribute`.

Para detectar el nombre real del campo de marca en Odoo:

```bash
curl 'http://localhost:8088/debug/odoo/product-fields?contains=brand'
curl 'http://localhost:8088/debug/odoo/product-fields?contains=marca'
```

Si el campo se llama distinto, ajustar `.env`:

```env
ODOO_BRAND_FIELD=nombre_del_campo
```

Para probar si WooCommerce expone endpoint de marcas:

```bash
curl 'http://localhost:8088/debug/woocommerce/brands'
```

El modo recomendado para marcas reales es:

```env
WC_BRAND_SYNC_MODE=brand
WC_BRAND_ENDPOINT=products/brands
```

Si WooCommerce o el plugin de marcas usa otro endpoint REST, ajustar `WC_BRAND_ENDPOINT`.

## Endpoints de verificacion

Health general:

```bash
curl http://localhost:8088/health
```

Health de Odoo:

```bash
curl http://localhost:8088/health/odoo
```

Health de WooCommerce:

```bash
curl http://localhost:8088/health/woocommerce
```

## Comandos de prueba y diagnostico

Ver logs del integrador:

```bash
docker logs -f odoo-woocommerce-integrator
```

Verificar variables cargadas en el contenedor:

```bash
docker exec -it odoo-woocommerce-integrator printenv ODOO_URL
docker exec -it odoo-woocommerce-integrator printenv ODOO_DB
docker exec -it odoo-woocommerce-integrator printenv ODOO_LANG
docker exec -it odoo-woocommerce-integrator printenv WC_URL
docker exec -it odoo-woocommerce-integrator printenv WEBHOOK_SECRET
```

Verificar resolucion DNS interna:

```bash
docker exec -it odoo-woocommerce-integrator python -c "import socket; print(socket.gethostbyname('odoo')); print(socket.gethostbyname('wordpress'))"
```

Verificar conectividad HTTP interna:

```bash
docker exec -it odoo-woocommerce-integrator curl -I http://odoo:8069
docker exec -it odoo-woocommerce-integrator curl -I http://wordpress
```

Probar WooCommerce directamente desde el servidor:

```bash
curl -i 'http://localhost:8080/wp-json/wc/v3/products?per_page=1&consumer_key=ck_xxx&consumer_secret=cs_xxx'
```

Resultado esperado:

```text
HTTP/1.1 200 OK
```

Si no hay productos, puede devolver una lista vacia:

```json
[]
```

Probar sincronizacion de pocos productos:

```bash
curl -X POST 'http://localhost:8088/sync/products?limit=5'
```

Probar sincronizacion de mas productos:

```bash
curl -X POST 'http://localhost:8088/sync/products?limit=20'
```

Probar sincronizacion de inventario sin pisar contenido web:

```bash
curl -X POST 'http://localhost:8088/sync/inventory?limit=500'
```

Este endpoint actualiza precio y stock desde Odoo hacia WooCommerce. No actualiza nombre, descripcion ni imagenes en productos existentes.

Probar sincronizacion de imagenes por SKU (CSV):

```bash
curl -X POST 'http://localhost:8088/sync/product-images' \
  -H 'Content-Type: text/csv' \
  --data-binary $'sku,image_url\nE-COM06,https://tu-cdn.com/img/e-com06.jpg\nE-COM07,https://tu-cdn.com/img/e-com07.jpg'
```

Probar sincronizacion de imagenes por SKU (JSON):

```bash
curl -X POST 'http://localhost:8088/sync/product-images' \
  -H 'Content-Type: application/json' \
  -d '[
    {"sku":"E-COM06","image_url":"https://tu-cdn.com/img/e-com06.jpg"},
    {"sku":"E-COM07","image_url":"https://tu-cdn.com/img/e-com07.jpg"}
  ]'
```

Este endpoint solo actualiza la galeria de imagenes del producto en WooCommerce usando `sku` como identificador. No modifica precio, stock ni configuracion de inventario automatica.

Tambien admite buscar imagen en internet por `marca + sku` cuando no viene `image_url`.

CSV con busqueda online:

```bash
curl -X POST 'http://localhost:8088/sync/product-images?search_online=true' \
  -H 'Content-Type: text/csv' \
  --data-binary $'sku,brand,name\nABC-001,Nike,Air Max 90\nABC-002,Adidas,Ultraboost'
```

JSON con busqueda online:

```bash
curl -X POST 'http://localhost:8088/sync/product-images?search_online=true' \
  -H 'Content-Type: application/json' \
  -d '[
    {"sku":"ABC-001","brand":"Nike","name":"Air Max 90"},
    {"sku":"ABC-002","brand":"Adidas","name":"Ultraboost"}
  ]'
```

Notas:

- Si `search_online=false`, el endpoint solo usa `image_url` provista en el payload.
- Si no encuentra imagen online, deja el SKU en `missing_image` y no toca stock ni precio.
- Si enviás un mapa de marcas, prioriza la `search_url` definida para la marca del XLSX antes del fallback general.

Tambien podes enviar un Excel `pagos.xlsx` (hoja 1) en vez de CSV.

Columnas recomendadas:

- `sku` (obligatoria)
- `brand` o `marca` (opcional, ayuda a la busqueda)
- `name` o `nombre` (opcional, ayuda a la busqueda)
- `image_url` (opcional; si falta y `search_online=true`, intenta encontrarla en internet)

Ejemplo con Excel:

```bash
curl -X POST 'http://localhost:8088/sync/product-images?search_online=true' \
  -H 'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' \
  --data-binary @pagos.xlsx
```

Tambien podés enviar el Excel junto con un CSV de fuentes de busqueda por marca usando `multipart/form-data`.

CSV de fuentes de busqueda (`brand_sources.csv`):

```csv
brand,search_url
BRAESI,https://baudinequipamientos.com/tienda/?q={sku}
LUNDAR,https://otro-sitio.com/buscar?q={sku}
GELOPAR,https://catalogo.ejemplo.com/search?term={brand}+{sku}
```

Placeholders soportados en `search_url`:

- `{sku}`
- `{brand}`
- `{name}`
- `{query}`: combina `brand + name + sku`

Ejemplo enviando `pagos.xlsx` + `brand_sources.csv`:

```bash
curl -X POST 'http://localhost:8088/sync/product-images?search_online=true' \
  -F 'products_file=@pagos.xlsx;type=application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' \
  -F 'brand_sources_file=@brand_sources.csv;type=text/csv'
```

Tambien podés usar un diccionario JSON inline:

```bash
curl -X POST 'http://localhost:8088/sync/product-images?search_online=true' \
  -H 'Content-Type: application/json' \
  -d '{
    "items": [
      {"sku":"m-160N","brand":"BRAESI","name":"Mesa de Acero Inoxidable 160"}
    ],
    "brand_sources": {
      "BRAESI": "https://baudinequipamientos.com/tienda/?q={sku}"
    }
  }'
```

Probar webhook manual con un pedido ignorado:

```bash
docker exec -it odoo-woocommerce-integrator curl -X POST \
  http://localhost:8088/webhooks/woocommerce/order \
  -H 'Content-Type: application/json' \
  -H 'X-Integrator-Secret: clave_larga_para_webhooks' \
  -d '{"id":123,"status":"pending","line_items":[]}'
```

Resultado esperado:

```json
{"ignored":true,"status":"pending"}
```

Probar reconciliacion de pedidos:

```bash
curl -X POST 'http://localhost:8088/sync/orders?limit=50'
```

Respuesta esperada:

```json
{
  "checked": 1,
  "synced": [
    {
      "woocommerce_id": 123,
      "odoo_sale_order_id": 45
    }
  ],
  "errors": []
}
```

Ver API keys de WooCommerce en MariaDB, ajustando el ID del contenedor si corresponde:

```bash
docker exec -it ID_MARIADB mysql -u wp_usuario -pPASSWORD wp_database -e "SELECT key_id, user_id, description, permissions, truncated_key, last_access FROM iuz_woocommerce_api_keys;"
```

Ver usuarios de WordPress:

```bash
docker exec -it ID_MARIADB mysql -u wp_usuario -pPASSWORD wp_database -e "SELECT ID, user_login, user_email FROM iuz_users;"
```

Ver rol/capabilities de un usuario:

```bash
docker exec -it ID_MARIADB mysql -u wp_usuario -pPASSWORD wp_database -e "SELECT meta_key, meta_value FROM iuz_usermeta WHERE user_id=1 AND meta_key LIKE '%capabilities%';"
```

Nota: en este WordPress el prefijo de tablas detectado fue `iuz_`. En otra instalacion puede ser distinto.

## Estado actual

Completado:

- Contenedor FastAPI funcionando.
- Conexion interna Docker con Odoo.
- Conexion interna Docker con WooCommerce.
- Autenticacion WooCommerce resuelta.
- Sincronizacion Odoo -> WooCommerce funcionando.
- Productos creados en WooCommerce desde Odoo por SKU.
- Reconciliacion manual de pedidos WooCommerce -> Odoo.
- Creacion idempotente de pedidos para evitar duplicados.
- Sincronizacion automatica opcional por intervalo.
- Sincronizacion segura de inventario sin pisar contenido web.

Pendiente para la siguiente etapa:

- Soporte avanzado para variantes, impuestos, categorias e imagenes.

## Flujo recomendado del proyecto

El flujo funcional buscado es:

```text
Odoo administra productos, precios y stock.
WooCommerce publica el catalogo y procesa ventas web.
El integrador sincroniza catalogo desde Odoo hacia WooCommerce.
WooCommerce avisa pedidos al integrador por webhook.
El integrador crea pedidos de venta en Odoo.
Odoo queda como fuente principal para inventario y gestion comercial.
```
e