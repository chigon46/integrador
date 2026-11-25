# Documentacion Operativa - Integrador Odoo WooCommerce (Sovai)

## 1) Objetivo
Este servicio integra Odoo y WooCommerce para:
- Sincronizar productos e inventario desde Odoo hacia WooCommerce.
- Sincronizar pedidos pagos desde WooCommerce hacia Odoo.
- Permitir actualizacion de imagenes por SKU con archivos CSV/JSON/XLSX.

Arquitectura:

Odoo -> Integrador FastAPI -> WordPress/WooCommerce

## 2) Requisitos previos
- Docker y Docker Compose instalados.
- Odoo y WordPress/WooCommerce funcionando en la red Docker compartida.
- API key de WooCommerce con permisos Read/Write.
- Archivo .env generado a partir de .env.example.

## 3) Variables de entorno
Copiar .env.example a .env y completar valores reales.

Variables principales:
- ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD
- WC_URL, WC_CONSUMER_KEY, WC_CONSUMER_SECRET
- WEBHOOK_SECRET
- BFL_API_KEY (opcional; requerido para generar imagenes de producto)
- IMAGE_GENERATION_LOGO_URL (default: logo oficial de SOVAI en sovai.com.uy)
- IMAGE_GENERATION_STORAGE_PATH (default: /app/generated_images)
- IMAGE_GENERATION_INTERNAL_BASE_URL (default: http://integrator.local:8088)

Sincronizacion automatica:
- ENABLE_BACKGROUND_SYNC
- BACKGROUND_SYNC_INTERVAL_SECONDS
- BACKGROUND_ORDERS_LIMIT
- BACKGROUND_PRODUCTS_LIMIT
- BACKGROUND_SYNC_ORDERS
- BACKGROUND_SYNC_INVENTORY

Marcas:
- ODOO_BRAND_FIELD (default: product_brand_id)
- WC_BRAND_SYNC_MODE (brand | attribute)
- WC_BRAND_ENDPOINT
- WC_BRAND_ATTRIBUTE_NAME

## 4) Build y despliegue con Docker (entorno actual)
1. Verificar que exista la red externa integration_net:

   docker network ls

2. Si no existe, crearla:

   docker network create integration_net

3. Construir la imagen manualmente (fuera de Compose):

  docker build -t odoo-woocommerce-integrator .

4. Levantar el contenedor con Compose apuntando a esa imagen:

  docker compose up -d

5. Ver logs:

   docker logs -f odoo-woocommerce-integrator

Servicio expuesto en:
- http://localhost:8088

Nota:
- En este entorno Compose no hace build.
- El archivo docker-compose.yaml usa image: odoo-woocommerce-integrator:latest.
- Si cambias codigo, reconstruir imagen y recrear contenedor:

  docker build -t odoo-woocommerce-integrator .
  docker compose up -d --force-recreate

## 5) Levantar en modo local (sin Docker)
1. Crear entorno virtual e instalar dependencias:

   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt

2. Ejecutar API:

   uvicorn app.main:app --host 0.0.0.0 --port 8088

Nota: en local, ODOO_URL y WC_URL deben ser accesibles desde tu host.

## 6) Pruebas de salud
- Health general:

  curl http://localhost:8088/health

- Health Odoo:

  curl http://localhost:8088/health/odoo

- Health WooCommerce:

  curl http://localhost:8088/health/woocommerce

- Estado de sincronizacion de fondo:

  curl http://localhost:8088/debug/sync-status

## 7) Endpoints clave de operacion
### 7.1 Productos (Odoo -> WooCommerce)
- Sincronizacion completa de contenido + inventario:

  POST /sync/products?limit=50&update_content=true

Ejemplo:

  curl -X POST "http://localhost:8088/sync/products?limit=50"

### 7.2 Inventario seguro (sin tocar contenido web)
- Solo precio/stock:

  POST /sync/inventory?limit=500

Ejemplo:

  curl -X POST "http://localhost:8088/sync/inventory?limit=500"

### 7.3 Pedidos (WooCommerce -> Odoo)
- Trae pedidos processing/completed y crea sale.order:

  POST /sync/orders?limit=50

Ejemplo:

  curl -X POST "http://localhost:8088/sync/orders?limit=50"

Para revisar qué pedidos devuelve WooCommerce y su estado de sincronizacion:

  GET /debug/woocommerce/orders?limit=50&status=any

Ejemplo:

  curl "http://localhost:8088/debug/woocommerce/orders?limit=50&status=any"

En cada pedido, revisar `sync.status`:
- `synced`: ya tiene un sale.order en Odoo; el integrador lo saltea.
- `sync_error`: fallo el intento anterior; se puede reintentar.
- `null`: WooCommerce no tiene metadata de sincronizacion.

Para revisar solamente pedidos en un estado:

  curl "http://localhost:8088/debug/woocommerce/orders?limit=50&status=processing"

Para comparar con los pedidos existentes en Odoo:

  curl "http://localhost:8088/debug/odoo/sale-orders?limit=50"

### 7.4 Webhook de pedido
- Endpoint:

  POST /webhooks/woocommerce/order

- Acepta validacion por firma WooCommerce o por header X-Integrator-Secret.

### 7.5 Imagenes por SKU

 Para generar y publicar automaticamente solo productos que ya tienen imagen, usar `generate_with_ai=true&only_with_image=true&auto_approve=true`. Para generar y publicar solo en productos que actualmente no tienen imagen, usar `generate_with_ai=true&only_without_image=true&auto_approve=true`; si no hay `image_url` ni se encuentra una imagen online, genera desde `name` o `descripcion`, `brand` y `sku`. Los productos que no coinciden con el filtro se informan en `skipped_no_image` o `skipped_with_image`. Los dos filtros no se pueden combinar.

Ejemplo de lote para completar productos sin imagen y publicar automaticamente:

  curl -X POST "http://localhost:8088/sync/product-images?generate_with_ai=true&only_without_image=true&auto_approve=true&limit=100&offset=0" \
    -H "X-Integrator-Secret: TU_WEBHOOK_SECRET" \
    -F "products_file=@sovai_lista_m.xlsx" \
    -o lote-sin-imagenes-0.json
 Las llamadas de generacion procesan como maximo 100 filas. Para continuar un archivo grande, usar `next_offset` de la respuesta como el `offset` de la llamada siguiente.

Ejemplo de prueba sin actualizar productos:

  curl -X POST "http://localhost:8088/sync/product-images?search_online=true&dry_run=true" \\
    -F "products_file=@sovai_lista_m.xlsx" \\
    -F "brand_sources_file=@brand_sources_validar.csv"

Ejemplo para generar una vista previa desde la imagen ya cargada en WooCommerce:

  curl -X POST "http://localhost:8088/sync/product-images?generate_with_ai=true" \
    -H "Content-Type: application/json" \
    -H "X-Integrator-Secret: TU_WEBHOOK_SECRET" \
    -d '[{"sku":"SKU-LIMPIO","brand":"BRAESI","name":"Producto"}]'

Si el producto no tiene imagen en WooCommerce, el generador puede crear una propuesta desde texto con `name` (o `descripcion`), `brand` y `sku`; no es necesario enviar `image_url`. La imagen será una interpretación aproximada de esos datos. `only_with_image=true` excluye los productos sin imagen y `only_without_image=true` excluye los que ya tienen imagen. En ese caso la respuesta indica `generation_mode: text_to_image` y `source_image_url` queda vacío.

La respuesta incluye `source_image_url` para confirmar la imagen de origen, `preview_url` para revisar el resultado y `approval_endpoint`. Para publicarla luego de revisarla:

  curl -X POST "http://localhost:8088/product-images/generations/ID_GENERACION/approve" \
    -H "X-Integrator-Secret: TU_WEBHOOK_SECRET"

  Ejemplo de lote de hasta 100 productos con imagen; edita la imagen actual y publica directamente:

    curl -X POST "http://localhost:8088/sync/product-images?generate_with_ai=true&only_with_image=true&auto_approve=true&limit=100&offset=0" \\
      -H "X-Integrator-Secret: TU_WEBHOOK_SECRET" \
      -F "products_file=@sovai_lista_m.xlsx" \
      -o lote-imagenes-0.json

  La autoaprobacion evita la revision manual. Probar primero con un archivo pequeno y revisar `synced`, `skipped_no_image` y `errors`. Cada generacion consume creditos BFL. Para continuar, usar el `next_offset` de la respuesta en la siguiente llamada.

La primera generacion requiere configurar `BFL_API_KEY` en `.env` con una clave del dashboard de Black Forest Labs y tener creditos disponibles. `IMAGE_GENERATION_LOGO_URL` puede apuntar a otro PNG publico si cambia el recurso de marca. El generador usa FLUX 3 Image por API, espera el resultado asincrono y lo descarga inmediatamente porque el enlace temporal expira. El contenedor conserva las vistas previas en un volumen Docker.

## 8) Archivos de entrada para imagenes
### CSV minimo
Encabezado obligatorio: sku

Ejemplo:

sku,image_url
E-COM06,https://tu-cdn.com/img/e-com06.jpg
E-COM07,https://tu-cdn.com/img/e-com07.jpg

### JSON minimo
[
  {"sku":"E-COM06","image_url":"https://tu-cdn.com/img/e-com06.jpg"},
  {"sku":"E-COM07","image_url":"https://tu-cdn.com/img/e-com07.jpg"}
]

### XLSX
Columnas recomendadas:
- sku (obligatoria)
- image_url (opcional)
- brand o marca (opcional)
- name o nombre (opcional)
- En `sovai_lista_m.xlsx`, `SKU limpio` se usa para encontrar el producto en WooCommerce, `SKU original` para buscar la imagen en fuentes externas y `Descripción` como nombre de búsqueda.

## 9) Operacion diaria recomendada
1. Validar healths.
2. Ejecutar sync/inventory para alinear precio y stock.
3. Ejecutar sync/orders para reconciliar pedidos.
4. Revisar respuesta de errores y metadatos en WooCommerce.
5. Revisar logs del contenedor.

Si background sync esta activado, usar sync/orders manual solo para contingencias.

## 10) Tipos de acciones operativas y como ejecutarlas
Esta seccion resume las acciones vistas en history y define la forma recomendada de operarlas.

### 10.1 Build de imagen del integrador
Cuándo usarla:
- Cuando hay cambios de codigo o dependencias.

Como se hace:
- Comando directo:

  docker build -t odoo-woocommerce-integrator .

Resultado esperado:
- Imagen disponible con tag odoo-woocommerce-integrator:latest.

### 10.2 Arranque/parada del integrador
Cuándo usarla:
- Inicio de servicio, mantenimiento, reinicio de despliegue.

Como se hace:
- Comandos directos:

  docker compose up -d
  docker compose down

Cuando hay nueva imagen:

  docker compose up -d --force-recreate

### 10.3 Verificacion de estado y logs
Cuándo usarla:
- Validar que el servicio quedo operativo luego de deploy o cambios.

Como se hace:
- Comandos directos:

  docker ps
  docker logs -f odoo-woocommerce-integrator

### 10.4 Health checks del integrador
Cuándo usarla:
- Confirmar conectividad API, Odoo y WooCommerce.

Como se hace:
- Comandos directos:

  curl http://localhost:8088/health
  curl http://localhost:8088/health/odoo
  curl http://localhost:8088/health/woocommerce

### 10.5 Sincronizacion de productos e inventario
Cuándo usarla:
- Luego de actualizaciones de catalogo/precios/stock.

Como se hace:
- Comandos directos:

  curl -X POST "http://localhost:8088/sync/products?limit=20"
  curl -X POST "http://localhost:8088/sync/inventory?limit=500"

Recomendacion:
- Para operacion diaria priorizar sync/inventory.
- Usar sync/products cuando se necesita refrescar tambien contenido.

### 10.6 Sincronizacion de pedidos
Cuándo usarla:
- Reconciliar pedidos pagos de WooCommerce hacia Odoo.

Como se hace:
- Comando directo:

  curl -X POST "http://localhost:8088/sync/orders?limit=50"

### 10.6.1 Cotizaciones del BCU (SOAP)
Cuándo usarla:
- Consultar cotizaciones del BCU para tasas de cambio o actualización de moneda.

Como se hace:
- Consulta directa del servicio:

  curl "http://localhost:8088/debug/bcu/cotizaciones?monedas=1,2&fecha_desde=2025-09-20&fecha_hasta=2025-09-22&grupo=1"

- Sincronizar cotizaciones a Odoo:

  curl -X POST "http://localhost:8088/sync/currency-rates?monedas=1,2&fecha_desde=2025-09-20&fecha_hasta=2025-09-22&grupo=1"

Observaciones:
- El servicio del BCU usa SOAP con WSDL en:

  https://cotizaciones.bcu.gub.uy/wscotizaciones/servlet/awsbcucotizaciones?wsdl

- El campo `monedas` acepta códigos numericos del BCU (por ejemplo `1` para USD, etc.) y el valor debe coincidir con el catálogo del servicio.
- La respuesta incluye `status`, `codigoerror`, `mensaje` e `items`, donde cada item trae `Fecha`, `Moneda`, `Nombre`, `CodigoISO`, `Emisor`, `TCC`, `TCV`, `ArbAct` y `FormaArbitrar`.
- El BCU puede devolver `status=1` junto con cotizaciones validas; en ese caso se deben revisar los elementos de `items`.
- Si `items` viene vacio, hay que revisar `codigoerror` y `mensaje`.
- La sincronizacion guarda cada cotizacion en Odoo en `res.currency.rate`, buscando la moneda por `CodigoISO` (`USD`, `EUR`, `ARS`, etc.).
- Si ya existe una tasa para la misma moneda y fecha, la actualiza; si no existe, la crea.
- El BCU informa TCC como moneda extranjera expresada en UYU y Odoo guarda la tasa inversa.

### 10.7 Prueba de webhook
Cuándo usarla:
- Validar ingreso de pedidos por webhook y seguridad de firma/secret.

Como se hace:
- Comando directo (ejemplo de prueba):

  curl -X POST http://localhost:8088/webhooks/woocommerce/order -H "Content-Type: application/json" -H "X-Integrator-Secret: TU_SECRET" -d '{"id":999,"status":"pending","line_items":[]}'

Nota de seguridad:
- No poner secretos reales en el comando si queda guardado en history.

### 10.8 Backup
Cuándo usarla:
- Resguardo periodico o previo a tareas de mantenimiento.

Como se hace:
- Opcion recomendada por script:

  sh scripts/backup.sh

- Opcion manual por comandos (solo administracion avanzada):
  - Configurar entorno de backup (ej. repositorio Restic).
  - Ejecutar comandos restic segun procedimiento del equipo.

Recomendacion:
- Estandarizar por script y no por comandos sueltos.
- Revisar siempre log posterior:

  tail -n 50 logs/backup.log

### 10.9 Recuperacion/volumenes Docker
Cuándo usarla:
- Solo incidentes o restauraciones controladas.

Como se hace:
- Comandos de inspeccion:

  docker volume ls
  docker inspect <container_id>

Advertencia:
- Evitar comandos destructivos sobre volumenes sin backup verificado.

### 10.10 Cambios de configuracion en nginx
Cuándo usarla:
- Ajustes de proxy, TLS, dominios y certificados.

Como se hace:
- Editar archivo de sitio y recargar/reiniciar:

  sudo vi /etc/nginx/conf.d/sovai.conf
  sudo systemctl reload nginx

Si falla reload:

  sudo systemctl restart nginx
  journalctl -xeu nginx.service

### 10.11 Reinicio de servicios base
Cuándo usarla:
- Fallas de runtime en Docker o sistema.

Como se hace:
- Docker daemon:

  sudo systemctl restart docker

- Servidor completo (ultimo recurso):

  sudo reboot

## 11) Solucion de problemas
### Error de autenticacion Odoo
- Revisar ODOO_DB, ODOO_USER, ODOO_PASSWORD.
- Probar /health/odoo.

### Error de autenticacion WooCommerce
- Revisar WC_CONSUMER_KEY y WC_CONSUMER_SECRET.
- Confirmar permisos Read/Write en WooCommerce.
- Probar /health/woocommerce.

### No sincroniza marca
- Revisar ODOO_BRAND_FIELD.
- Probar /debug/odoo/product-fields?contains=brand.
- Revisar WC_BRAND_SYNC_MODE y WC_BRAND_ENDPOINT.
- Probar /debug/woocommerce/brands.

### Pedidos duplicados o reintentos
- La deduplicacion se hace por client_order_ref = "WooCommerce #ID".
- Revisar metadatos en WooCommerce:
  - _odoo_sale_order_id
  - _odoo_sync_status
  - _odoo_sync_error

### Integrador no llega a Odoo/WordPress por nombre DNS
- Confirmar red Docker integration_net compartida.
- Validar ODOO_URL y WC_URL (usando nombres de servicio correctos).

## 12) Endpoints de debug utiles
- GET /debug/odoo/sale-orders?limit=10
- GET /debug/odoo/product-fields?contains=brand
- GET /debug/woocommerce/brands?search=texto
- GET /debug/woocommerce/orders?limit=50&status=any
- GET /debug/bcu/cotizaciones?monedas=1,2&fecha_desde=2025-09-20&fecha_hasta=2025-09-22&grupo=1
- GET /debug/sync-status

## 13) Archivos relevantes del proyecto
- app/main.py: endpoints y flujo de sincronizacion.
- app/odoo_client.py: cliente XML-RPC de Odoo.
- app/woocommerce_client.py: cliente REST de WooCommerce.
- app/image_finder.py: busqueda de imagenes online.
- app/settings.py: configuracion por variables de entorno.
- docker-compose.yaml: despliegue del servicio.
- Dockerfile: imagen del integrador.
- .env.example: plantilla de configuracion.

## 14) Checklist de salida a produccion
- Variables de entorno completas y validadas.
- API keys de WooCommerce activas y con permisos correctos.
- Health endpoints en OK.
- Prueba de sync/products con limit bajo (5).
- Prueba de sync/orders con limit bajo (5).
- Politica definida para ENABLE_BACKGROUND_SYNC.
- WEBHOOK_SECRET robusto y privado.
- Logs monitoreados durante las primeras corridas.
