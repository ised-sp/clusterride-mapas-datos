INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;
SET s3_region='us-west-2';
CREATE TABLE seg AS
SELECT id, class, subclass, speed_limits, sources,
       ST_Length_Spheroid(ST_FlipCoordinates(geometry)) / 1000 AS km
FROM read_parquet(getenv('OVERTURE') || '/theme=transportation/type=segment/*', hive_partitioning=1)
WHERE subtype = 'road'
  AND bbox.xmin > -9.5 AND bbox.xmax < 4.5 AND bbox.ymin > 35.8 AND bbox.ymax < 43.9
  AND class IN ('motorway','trunk','primary','secondary','tertiary','unclassified','residential');
COPY (
  SELECT class,
         round(sum(km)) AS km,
         round(100 * sum(km) FILTER (WHERE speed_limits IS NOT NULL AND len(speed_limits) > 0) / sum(km), 1) AS limite_pct,
         round(100 * sum(km) FILTER (WHERE list_contains(list_transform(sources, s -> s.dataset <> 'OpenStreetMap' AND s.property LIKE '%speed%'), true)) / sum(km), 1) AS limite_no_osm_pct
  FROM seg GROUP BY class ORDER BY km DESC
) TO 'overture.csv' (HEADER);
COPY (SELECT s.dataset, s.property, count(*) n FROM (SELECT unnest(sources) s FROM seg) GROUP BY ALL ORDER BY n DESC LIMIT 40) TO 'fuentes.csv' (HEADER);
