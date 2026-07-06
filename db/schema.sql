-- Claude Memory Database Schema
-- PostgreSQL 16 + pgvector
--
-- GENERATED FILE - do not hand-edit table definitions here.
-- Regenerated 2026-07-06 (post-P1) by applying the full migration chain to a
-- fresh database and dumping it:
--   migrations/001_initial_schema.sql
--   migrations/002_v2_features.sql
--   migrations/003_v3_codified_context.sql
--   db/migrations/v4_feedback_loop.sql
--   db/migrations/v5_consolidation.sql
--   db/migrations/v5_1_backlog_analysis.sql
--   db/migrations/v5_oauth_persistence.sql
--   db/migrations/v6_attribution.sql
--   db/migrations/v7_complement_verdict.sql
--   db/migrations/v8_client_last_seen.sql
--   db/migrations/v9_recency_ranking.sql
-- (db/migrations/001_add_journal.sql is a redundant IF-NOT-EXISTS predecessor
--  of the journal table already created in 001_initial_schema.sql.)
--
-- This file is the fresh-database bootstrap: docker-compose.yml mounts it as
-- /docker-entrypoint-initdb.d/01-schema.sql. A fresh bootstrap needs no
-- further migrations; existing databases apply new migrations incrementally.
--
-- To regenerate after adding a migration:
--   1. Apply the chain (or just the new migration) to a scratch database
--   2. pg_dump -U claude -d <scratch> --schema-only --no-owner --no-privileges
--   3. Strip \restrict/\unrestrict lines; replace everything below this header

--
-- PostgreSQL database dump
--


-- Dumped from database version 16.13 (Debian 16.13-1.pgdg12+1)
-- Dumped by pg_dump version 16.13 (Debian 16.13-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: uuid-ossp; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;


--
-- Name: EXTENSION "uuid-ossp"; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION "uuid-ossp" IS 'generate universally unique identifiers (UUIDs)';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: agent_specs_tsv_trigger(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.agent_specs_tsv_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.name, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.description, '') || ' ' || COALESCE(NEW.summary, '')), 'B');
    RETURN NEW;
END;
$$;


--
-- Name: journal_tsv_trigger(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.journal_tsv_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.content, '')), 'B');
    RETURN NEW;
END;
$$;


--
-- Name: lessons_tsv_trigger(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.lessons_tsv_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.content, '')), 'B');
    RETURN NEW;
END;
$$;


--
-- Name: mcp_server_tools_tsv_trigger(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.mcp_server_tools_tsv_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.tool_name, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.description, '')), 'B');
    RETURN NEW;
END;
$$;


--
-- Name: memory_rank(double precision, double precision, double precision); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.memory_rank(similarity double precision, keyword_boost double precision, age_days double precision) RETURNS double precision
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT (similarity + keyword_boost)
           * (0.7 + 0.3 * exp(-GREATEST(COALESCE(age_days, 365.0), 0.0) / 180.0))
$$;


--
-- Name: patterns_tsv_trigger(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.patterns_tsv_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.name, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.problem, '') || ' ' || COALESCE(NEW.solution, '')), 'B');
    RETURN NEW;
END;
$$;


--
-- Name: semantic_search(public.vector, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.semantic_search(query_embedding public.vector, search_limit integer DEFAULT 5) RETURNS TABLE(source_type text, source_id integer, title text, content text, similarity double precision)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    SELECT * FROM (
        SELECT
            'lesson'::TEXT as source_type,
            l.id as source_id,
            l.title::TEXT as title,
            l.content::TEXT as content,
            1 - (l.embedding <=> query_embedding) as similarity
        FROM lessons l
        WHERE l.embedding IS NOT NULL AND l.retired_at IS NULL

        UNION ALL

        SELECT
            'pattern'::TEXT as source_type,
            p.id as source_id,
            p.name::TEXT as title,
            p.problem::TEXT as content,
            1 - (p.embedding <=> query_embedding) as similarity
        FROM patterns p
        WHERE p.embedding IS NOT NULL

        UNION ALL

        SELECT
            'session'::TEXT as source_type,
            s.id as source_id,
            'Session ' || s.id::TEXT as title,
            s.summary::TEXT as content,
            1 - (s.embedding <=> query_embedding) as similarity
        FROM sessions s
        WHERE s.embedding IS NOT NULL

        UNION ALL

        SELECT
            'journal'::TEXT as source_type,
            j.id as source_id,
            'Journal ' || to_char(j.entry_date, 'YYYY-MM-DD')::TEXT as title,
            j.content::TEXT as content,
            1 - (j.embedding <=> query_embedding) as similarity
        FROM journal j
        WHERE j.embedding IS NOT NULL

        UNION ALL

        SELECT
            'agent_spec'::TEXT as source_type,
            a.id as source_id,
            a.name::TEXT as title,
            COALESCE(a.summary, a.description)::TEXT as content,
            1 - (a.embedding <=> query_embedding) as similarity
        FROM agent_specs a
        WHERE a.embedding IS NOT NULL AND a.retired_at IS NULL

        UNION ALL

        SELECT
            'specification'::TEXT as source_type,
            sp.id as source_id,
            sp.title::TEXT as title,
            COALESCE(sp.summary, sp.title)::TEXT as content,
            1 - (sp.embedding <=> query_embedding) as similarity
        FROM specifications sp
        WHERE sp.embedding IS NOT NULL AND sp.retired_at IS NULL

        UNION ALL

        SELECT
            'mcp_server'::TEXT as source_type,
            ms.id as source_id,
            ms.name::TEXT as title,
            ms.description::TEXT as content,
            1 - (ms.embedding <=> query_embedding) as similarity
        FROM mcp_servers ms
        WHERE ms.embedding IS NOT NULL AND ms.retired_at IS NULL

        UNION ALL

        SELECT
            'mcp_tool'::TEXT as source_type,
            mt.id as source_id,
            mt.tool_name::TEXT as title,
            mt.description::TEXT as content,
            1 - (mt.embedding <=> query_embedding) as similarity
        FROM mcp_server_tools mt
        WHERE mt.embedding IS NOT NULL
    ) combined
    ORDER BY similarity DESC
    LIMIT search_limit;
END;
$$;


--
-- Name: semantic_search(public.vector, text, integer, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.semantic_search(query_embedding public.vector, query_text text, search_limit integer DEFAULT 5, filter_source_agent text DEFAULT NULL::text) RETURNS TABLE(source_type text, source_id integer, title text, content text, similarity double precision, keyword_boost double precision, effective_score double precision, upvotes integer, downvotes integer)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    SELECT * FROM (
        -- Lessons
        SELECT
            'lesson'::TEXT AS source_type,
            l.id AS source_id,
            l.title::TEXT AS title,
            l.content::TEXT AS content,
            (1 - (l.embedding <=> query_embedding))::FLOAT AS similarity,
            (CASE
                WHEN l.tsv @@ plainto_tsquery('english', query_text)
                THEN ts_rank(l.tsv, plainto_tsquery('english', query_text)) * 0.3
                ELSE 0.0
            END)::FLOAT AS keyword_boost,
            memory_rank(
                (1 - (l.embedding <=> query_embedding))::FLOAT,
                (CASE
                    WHEN l.tsv @@ plainto_tsquery('english', query_text)
                    THEN ts_rank(l.tsv, plainto_tsquery('english', query_text)) * 0.3
                    ELSE 0.0
                END)::FLOAT,
                (EXTRACT(EPOCH FROM (NOW() - l.learned_at)) / 86400.0)::FLOAT
            )::FLOAT AS effective_score,
            l.upvotes AS upvotes,
            l.downvotes AS downvotes
        FROM lessons l
        WHERE l.embedding IS NOT NULL AND l.retired_at IS NULL
          AND (filter_source_agent IS NULL OR l.source_agent = filter_source_agent)

        UNION ALL

        -- Patterns
        SELECT
            'pattern'::TEXT, p.id, p.name::TEXT, p.problem::TEXT,
            (1 - (p.embedding <=> query_embedding))::FLOAT,
            (CASE
                WHEN p.tsv @@ plainto_tsquery('english', query_text)
                THEN ts_rank(p.tsv, plainto_tsquery('english', query_text)) * 0.3
                ELSE 0.0
            END)::FLOAT,
            memory_rank(
                (1 - (p.embedding <=> query_embedding))::FLOAT,
                (CASE
                    WHEN p.tsv @@ plainto_tsquery('english', query_text)
                    THEN ts_rank(p.tsv, plainto_tsquery('english', query_text)) * 0.3
                    ELSE 0.0
                END)::FLOAT,
                (EXTRACT(EPOCH FROM (NOW() - p.created_at)) / 86400.0)::FLOAT
            )::FLOAT,
            0, 0
        FROM patterns p
        WHERE p.embedding IS NOT NULL
          AND (filter_source_agent IS NULL OR p.source_agent = filter_source_agent)

        UNION ALL

        -- Sessions
        SELECT
            'session'::TEXT, s.id, ('Session ' || s.id)::TEXT, s.summary::TEXT,
            (1 - (s.embedding <=> query_embedding))::FLOAT,
            (CASE
                WHEN s.tsv @@ plainto_tsquery('english', query_text)
                THEN ts_rank(s.tsv, plainto_tsquery('english', query_text)) * 0.3
                ELSE 0.0
            END)::FLOAT,
            memory_rank(
                (1 - (s.embedding <=> query_embedding))::FLOAT,
                (CASE
                    WHEN s.tsv @@ plainto_tsquery('english', query_text)
                    THEN ts_rank(s.tsv, plainto_tsquery('english', query_text)) * 0.3
                    ELSE 0.0
                END)::FLOAT,
                (EXTRACT(EPOCH FROM (NOW() - COALESCE(s.ended_at, s.started_at))) / 86400.0)::FLOAT
            )::FLOAT,
            0, 0
        FROM sessions s
        WHERE s.embedding IS NOT NULL
          AND (filter_source_agent IS NULL OR s.source_agent = filter_source_agent)

        UNION ALL

        -- Journal
        SELECT
            'journal'::TEXT, j.id,
            ('Journal ' || to_char(j.entry_date, 'YYYY-MM-DD'))::TEXT, j.content::TEXT,
            (1 - (j.embedding <=> query_embedding))::FLOAT,
            (CASE
                WHEN j.tsv @@ plainto_tsquery('english', query_text)
                THEN ts_rank(j.tsv, plainto_tsquery('english', query_text)) * 0.3
                ELSE 0.0
            END)::FLOAT,
            memory_rank(
                (1 - (j.embedding <=> query_embedding))::FLOAT,
                (CASE
                    WHEN j.tsv @@ plainto_tsquery('english', query_text)
                    THEN ts_rank(j.tsv, plainto_tsquery('english', query_text)) * 0.3
                    ELSE 0.0
                END)::FLOAT,
                (EXTRACT(EPOCH FROM (NOW() - j.entry_date)) / 86400.0)::FLOAT
            )::FLOAT,
            0, 0
        FROM journal j
        WHERE j.embedding IS NOT NULL
          AND (filter_source_agent IS NULL OR j.source_agent = filter_source_agent)
    ) combined
    ORDER BY effective_score DESC
    LIMIT search_limit;
END;
$$;


--
-- Name: sessions_tsv_trigger(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.sessions_tsv_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.summary, '')), 'B');
    RETURN NEW;
END;
$$;


--
-- Name: specifications_tsv_trigger(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.specifications_tsv_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.summary, '')), 'B');
    RETURN NEW;
END;
$$;


--
-- Name: update_updated_at_column(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_updated_at_column() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_specs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_specs (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    description text NOT NULL,
    spec_content text NOT NULL,
    summary text,
    model character varying(20) DEFAULT 'sonnet'::character varying,
    triggers text[] DEFAULT '{}'::text[],
    tools text[] DEFAULT '{}'::text[],
    project_id integer,
    version integer DEFAULT 1,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    retired_at timestamp without time zone,
    retired_reason text,
    embedding public.vector(1536),
    tsv tsvector,
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text,
    retired_by_agent text,
    updated_by_agent text
);


--
-- Name: agent_specs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_specs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_specs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_specs_id_seq OWNED BY public.agent_specs.id;


--
-- Name: annotations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.annotations (
    id integer NOT NULL,
    entity_type character varying(50) NOT NULL,
    entity_id integer NOT NULL,
    note text NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text,
    retired_by_agent text,
    updated_by_agent text
);


--
-- Name: annotations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.annotations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: annotations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.annotations_id_seq OWNED BY public.annotations.id;


--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_keys (
    id integer NOT NULL,
    api_key_hash text NOT NULL,
    family text NOT NULL,
    client_name text,
    label text,
    scopes text[] DEFAULT ARRAY['read'::text, 'write'::text] NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp without time zone,
    revoked_at timestamp without time zone
);


--
-- Name: api_keys_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.api_keys_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: api_keys_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.api_keys_id_seq OWNED BY public.api_keys.id;


--
-- Name: approaches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.approaches (
    id integer NOT NULL,
    project_id integer,
    area character varying(100) NOT NULL,
    current_approach text NOT NULL,
    previous_approach text,
    reason_for_change text,
    changed_at timestamp without time zone DEFAULT now(),
    status character varying(20) DEFAULT 'current'::character varying,
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text
);


--
-- Name: approaches_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.approaches_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: approaches_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.approaches_id_seq OWNED BY public.approaches.id;


--
-- Name: backlog_analysis; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.backlog_analysis (
    id integer NOT NULL,
    batch_run_id character varying(100) NOT NULL,
    lesson_a_id integer NOT NULL,
    lesson_b_id integer NOT NULL,
    cosine_similarity numeric(4,3) NOT NULL,
    judge_model character varying(50) NOT NULL,
    verdict character varying(20) NOT NULL,
    direction character varying(30),
    confidence numeric(3,2) NOT NULL,
    reasoning text NOT NULL,
    judged_at timestamp without time zone DEFAULT now(),
    left_source_agent text,
    right_source_agent text,
    cross_agent boolean GENERATED ALWAYS AS ((left_source_agent IS DISTINCT FROM right_source_agent)) STORED,
    CONSTRAINT backlog_analysis_check CHECK ((lesson_a_id < lesson_b_id)),
    CONSTRAINT backlog_analysis_verdict_check CHECK (((verdict)::text = ANY ((ARRAY['duplicate'::character varying, 'supersedes'::character varying, 'complement'::character varying, 'contradicts'::character varying, 'unrelated'::character varying])::text[])))
);


--
-- Name: backlog_analysis_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.backlog_analysis_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: backlog_analysis_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.backlog_analysis_id_seq OWNED BY public.backlog_analysis.id;


--
-- Name: consolidation_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.consolidation_queue (
    id integer NOT NULL,
    new_lesson_id integer NOT NULL,
    candidate_lesson_id integer NOT NULL,
    proposed_action character varying(20) NOT NULL,
    proposed_direction character varying(30),
    judge_model character varying(50) NOT NULL,
    judge_confidence numeric(3,2) NOT NULL,
    judge_reasoning text NOT NULL,
    cosine_similarity numeric(3,2) NOT NULL,
    enqueued_at timestamp without time zone DEFAULT now(),
    decided_at timestamp without time zone,
    decided_by character varying(100),
    decision character varying(20),
    decision_note text,
    CONSTRAINT consolidation_queue_check CHECK ((new_lesson_id <> candidate_lesson_id)),
    CONSTRAINT consolidation_queue_decision_check CHECK (((decision)::text = ANY ((ARRAY['approved'::character varying, 'rejected'::character varying])::text[]))),
    CONSTRAINT consolidation_queue_proposed_action_check CHECK (((proposed_action)::text = ANY ((ARRAY['merged'::character varying, 'superseded'::character varying])::text[])))
);


--
-- Name: consolidation_queue_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.consolidation_queue_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: consolidation_queue_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.consolidation_queue_id_seq OWNED BY public.consolidation_queue.id;


--
-- Name: containers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.containers (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    machine_id integer,
    compose_path text,
    ports text,
    project character varying(100),
    status character varying(20) DEFAULT 'running'::character varying,
    created_at timestamp without time zone DEFAULT now(),
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text
);


--
-- Name: containers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.containers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: containers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.containers_id_seq OWNED BY public.containers.id;


--
-- Name: databases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.databases (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    db_type character varying(20) NOT NULL,
    machine_id integer,
    connection_hint text,
    project character varying(100),
    created_at timestamp without time zone DEFAULT now(),
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text
);


--
-- Name: databases_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.databases_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: databases_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.databases_id_seq OWNED BY public.databases.id;


--
-- Name: guardrails; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.guardrails (
    id integer NOT NULL,
    project_id integer,
    description text NOT NULL,
    check_type character varying(50) DEFAULT 'always'::character varying,
    file_path text,
    pattern text,
    severity character varying(20) DEFAULT 'warning'::character varying,
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text
);


--
-- Name: guardrails_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.guardrails_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: guardrails_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.guardrails_id_seq OWNED BY public.guardrails.id;


--
-- Name: journal; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.journal (
    id integer NOT NULL,
    entry_date timestamp without time zone DEFAULT now(),
    content text NOT NULL,
    tags text[] DEFAULT '{}'::text[],
    mood character varying(50),
    project_id integer,
    session_id integer,
    embedding public.vector(1536),
    tsv tsvector,
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text,
    retired_by_agent text,
    updated_by_agent text
);


--
-- Name: journal_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.journal_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: journal_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.journal_id_seq OWNED BY public.journal.id;


--
-- Name: key_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.key_files (
    id integer NOT NULL,
    project_id integer,
    file_path text NOT NULL,
    line_hint integer,
    description text,
    importance character varying(20) DEFAULT 'reference'::character varying,
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text
);


--
-- Name: key_files_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.key_files_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: key_files_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.key_files_id_seq OWNED BY public.key_files.id;


--
-- Name: lesson_conflicts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lesson_conflicts (
    id integer NOT NULL,
    lesson_a_id integer NOT NULL,
    lesson_b_id integer NOT NULL,
    judge_model character varying(50) NOT NULL,
    judge_confidence numeric(3,2) NOT NULL,
    judge_reasoning text NOT NULL,
    cosine_similarity numeric(3,2) NOT NULL,
    flagged_at timestamp without time zone DEFAULT now(),
    resolved_at timestamp without time zone,
    resolved_by character varying(100),
    resolution character varying(20),
    resolution_note text,
    CONSTRAINT lesson_conflicts_check CHECK ((lesson_a_id < lesson_b_id)),
    CONSTRAINT lesson_conflicts_resolution_check CHECK (((resolution)::text = ANY ((ARRAY['kept_a'::character varying, 'kept_b'::character varying, 'kept_both'::character varying, 'irrelevant'::character varying])::text[])))
);


--
-- Name: lesson_conflicts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.lesson_conflicts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: lesson_conflicts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.lesson_conflicts_id_seq OWNED BY public.lesson_conflicts.id;


--
-- Name: lesson_merges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lesson_merges (
    id integer NOT NULL,
    canonical_id integer NOT NULL,
    merged_id integer NOT NULL,
    action character varying(20) NOT NULL,
    judge_model character varying(50) NOT NULL,
    judge_confidence numeric(3,2) NOT NULL,
    judge_reasoning text NOT NULL,
    cosine_similarity numeric(3,2) NOT NULL,
    auto_decided boolean NOT NULL,
    decided_by character varying(100),
    transferred_upvotes integer DEFAULT 0 NOT NULL,
    transferred_downvotes integer DEFAULT 0 NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    reversed_at timestamp without time zone,
    reversed_by character varying(100),
    reversed_reason text,
    CONSTRAINT lesson_merges_action_check CHECK (((action)::text = ANY ((ARRAY['merged'::character varying, 'superseded'::character varying])::text[]))),
    CONSTRAINT lesson_merges_check CHECK (((auto_decided = true) OR (decided_by IS NOT NULL))),
    CONSTRAINT lesson_merges_check1 CHECK ((canonical_id <> merged_id))
);


--
-- Name: lesson_merges_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.lesson_merges_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: lesson_merges_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.lesson_merges_id_seq OWNED BY public.lesson_merges.id;


--
-- Name: lessons; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lessons (
    id integer NOT NULL,
    title character varying(200) NOT NULL,
    content text NOT NULL,
    project_id integer,
    tags text[] DEFAULT '{}'::text[],
    severity character varying(20) DEFAULT 'tip'::character varying,
    learned_at timestamp without time zone DEFAULT now(),
    embedding public.vector(1536),
    retired_at timestamp without time zone,
    retired_reason text,
    upvotes integer DEFAULT 0,
    downvotes integer DEFAULT 0,
    last_rated_at timestamp without time zone,
    tsv tsvector,
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text,
    retired_by_agent text,
    updated_by_agent text
);


--
-- Name: lessons_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.lessons_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: lessons_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.lessons_id_seq OWNED BY public.lessons.id;


--
-- Name: machines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.machines (
    id integer NOT NULL,
    name character varying(50) NOT NULL,
    ip character varying(50),
    ssh_command text,
    notes text,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text
);


--
-- Name: machines_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.machines_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: machines_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.machines_id_seq OWNED BY public.machines.id;


--
-- Name: mcp_server_projects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_server_projects (
    server_id integer NOT NULL,
    project_id integer NOT NULL
);


--
-- Name: mcp_server_tools; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_server_tools (
    id integer NOT NULL,
    server_id integer NOT NULL,
    tool_name character varying(100) NOT NULL,
    description text NOT NULL,
    parameters jsonb,
    created_at timestamp without time zone DEFAULT now(),
    embedding public.vector(1536),
    tsv tsvector,
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text
);


--
-- Name: mcp_server_tools_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mcp_server_tools_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: mcp_server_tools_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mcp_server_tools_id_seq OWNED BY public.mcp_server_tools.id;


--
-- Name: mcp_servers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_servers (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    description text NOT NULL,
    url text,
    transport character varying(20) NOT NULL,
    machine_id integer,
    auth_type character varying(20) DEFAULT 'none'::character varying,
    auth_hint text,
    config_snippet jsonb,
    limitations text,
    status character varying(20) DEFAULT 'active'::character varying,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    retired_at timestamp without time zone,
    retired_reason text,
    embedding public.vector(1536),
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text,
    retired_by_agent text,
    updated_by_agent text
);


--
-- Name: mcp_servers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mcp_servers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: mcp_servers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mcp_servers_id_seq OWNED BY public.mcp_servers.id;


--
-- Name: oauth_access_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_access_tokens (
    token character varying(200) NOT NULL,
    client_id character varying(100),
    scopes jsonb DEFAULT '[]'::jsonb,
    expires_at bigint,
    resource character varying(500),
    created_at timestamp without time zone DEFAULT now()
);


--
-- Name: oauth_client_family; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_client_family (
    client_id text NOT NULL,
    family text NOT NULL,
    client_name text,
    inferred_from text NOT NULL,
    inferred_at timestamp without time zone DEFAULT now() NOT NULL
);


--
-- Name: oauth_clients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_clients (
    client_id character varying(100) NOT NULL,
    client_secret character varying(200),
    client_name character varying(200),
    redirect_uris jsonb DEFAULT '[]'::jsonb,
    grant_types jsonb DEFAULT '[]'::jsonb,
    response_types jsonb DEFAULT '[]'::jsonb,
    token_endpoint_auth_method character varying(50) DEFAULT 'client_secret_post'::character varying,
    client_id_issued_at bigint,
    raw_data jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    last_seen_at timestamp without time zone
);


--
-- Name: oauth_refresh_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_refresh_tokens (
    token character varying(200) NOT NULL,
    client_id character varying(100),
    scopes jsonb DEFAULT '[]'::jsonb,
    expires_at bigint,
    created_at timestamp without time zone DEFAULT now()
);


--
-- Name: patterns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.patterns (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    problem text NOT NULL,
    solution text NOT NULL,
    code_example text,
    applies_to text[] DEFAULT '{}'::text[],
    created_at timestamp without time zone DEFAULT now(),
    embedding public.vector(1536),
    tsv tsvector,
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text,
    retired_by_agent text,
    updated_by_agent text
);


--
-- Name: patterns_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.patterns_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: patterns_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.patterns_id_seq OWNED BY public.patterns.id;


--
-- Name: permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.permissions (
    id integer NOT NULL,
    scope character varying(50) DEFAULT 'global'::character varying,
    action_type character varying(100) NOT NULL,
    pattern text,
    allowed boolean DEFAULT true,
    requires_confirmation boolean DEFAULT false,
    notes text,
    created_at timestamp without time zone DEFAULT now(),
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text
);


--
-- Name: permissions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.permissions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: permissions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.permissions_id_seq OWNED BY public.permissions.id;


--
-- Name: project_aliases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_aliases (
    id integer NOT NULL,
    alias character varying(100) NOT NULL,
    project_id integer,
    created_at timestamp without time zone DEFAULT now(),
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text
);


--
-- Name: project_aliases_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.project_aliases_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: project_aliases_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.project_aliases_id_seq OWNED BY public.project_aliases.id;


--
-- Name: project_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_state (
    project_id integer NOT NULL,
    last_session_id integer,
    current_focus text,
    blockers text[] DEFAULT '{}'::text[],
    next_steps text[] DEFAULT '{}'::text[],
    updated_at timestamp without time zone DEFAULT now(),
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text
);


--
-- Name: projects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.projects (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    path text,
    machine_id integer,
    status character varying(20) DEFAULT 'active'::character varying,
    tech_stack jsonb DEFAULT '{}'::jsonb,
    current_phase text,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    claude_md text,
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text
);


--
-- Name: projects_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.projects_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: projects_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.projects_id_seq OWNED BY public.projects.id;


--
-- Name: session_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.session_items (
    id integer NOT NULL,
    session_id integer,
    item_type character varying(50) NOT NULL,
    description text NOT NULL,
    file_paths text[] DEFAULT '{}'::text[],
    created_at timestamp without time zone DEFAULT now()
);


--
-- Name: session_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.session_items_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: session_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.session_items_id_seq OWNED BY public.session_items.id;


--
-- Name: sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sessions (
    id integer NOT NULL,
    started_at timestamp without time zone DEFAULT now(),
    ended_at timestamp without time zone,
    machine_id integer,
    project_id integer,
    summary text,
    embedding public.vector(1536),
    tsv tsvector,
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text
);


--
-- Name: sessions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.sessions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sessions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.sessions_id_seq OWNED BY public.sessions.id;


--
-- Name: specifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.specifications (
    id integer NOT NULL,
    title character varying(200) NOT NULL,
    subsystem character varying(100),
    content text NOT NULL,
    summary text,
    format_hints text[] DEFAULT '{}'::text[],
    triggers text[] DEFAULT '{}'::text[],
    project_id integer NOT NULL,
    version integer DEFAULT 1,
    verified_at timestamp without time zone DEFAULT now(),
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    retired_at timestamp without time zone,
    retired_reason text,
    embedding public.vector(1536),
    tsv tsvector,
    source_agent text DEFAULT 'claude'::text NOT NULL,
    source_client_id text,
    retired_by_agent text,
    updated_by_agent text
);


--
-- Name: specifications_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.specifications_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: specifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.specifications_id_seq OWNED BY public.specifications.id;


--
-- Name: workflows; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflows (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    description text,
    steps jsonb DEFAULT '[]'::jsonb,
    tools_used text[] DEFAULT '{}'::text[],
    project_id integer,
    created_at timestamp without time zone DEFAULT now()
);


--
-- Name: workflows_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.workflows_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: workflows_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.workflows_id_seq OWNED BY public.workflows.id;


--
-- Name: agent_specs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_specs ALTER COLUMN id SET DEFAULT nextval('public.agent_specs_id_seq'::regclass);


--
-- Name: annotations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.annotations ALTER COLUMN id SET DEFAULT nextval('public.annotations_id_seq'::regclass);


--
-- Name: api_keys id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys ALTER COLUMN id SET DEFAULT nextval('public.api_keys_id_seq'::regclass);


--
-- Name: approaches id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approaches ALTER COLUMN id SET DEFAULT nextval('public.approaches_id_seq'::regclass);


--
-- Name: backlog_analysis id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backlog_analysis ALTER COLUMN id SET DEFAULT nextval('public.backlog_analysis_id_seq'::regclass);


--
-- Name: consolidation_queue id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consolidation_queue ALTER COLUMN id SET DEFAULT nextval('public.consolidation_queue_id_seq'::regclass);


--
-- Name: containers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.containers ALTER COLUMN id SET DEFAULT nextval('public.containers_id_seq'::regclass);


--
-- Name: databases id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.databases ALTER COLUMN id SET DEFAULT nextval('public.databases_id_seq'::regclass);


--
-- Name: guardrails id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guardrails ALTER COLUMN id SET DEFAULT nextval('public.guardrails_id_seq'::regclass);


--
-- Name: journal id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal ALTER COLUMN id SET DEFAULT nextval('public.journal_id_seq'::regclass);


--
-- Name: key_files id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.key_files ALTER COLUMN id SET DEFAULT nextval('public.key_files_id_seq'::regclass);


--
-- Name: lesson_conflicts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_conflicts ALTER COLUMN id SET DEFAULT nextval('public.lesson_conflicts_id_seq'::regclass);


--
-- Name: lesson_merges id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_merges ALTER COLUMN id SET DEFAULT nextval('public.lesson_merges_id_seq'::regclass);


--
-- Name: lessons id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons ALTER COLUMN id SET DEFAULT nextval('public.lessons_id_seq'::regclass);


--
-- Name: machines id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machines ALTER COLUMN id SET DEFAULT nextval('public.machines_id_seq'::regclass);


--
-- Name: mcp_server_tools id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_server_tools ALTER COLUMN id SET DEFAULT nextval('public.mcp_server_tools_id_seq'::regclass);


--
-- Name: mcp_servers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_servers ALTER COLUMN id SET DEFAULT nextval('public.mcp_servers_id_seq'::regclass);


--
-- Name: patterns id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patterns ALTER COLUMN id SET DEFAULT nextval('public.patterns_id_seq'::regclass);


--
-- Name: permissions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.permissions ALTER COLUMN id SET DEFAULT nextval('public.permissions_id_seq'::regclass);


--
-- Name: project_aliases id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_aliases ALTER COLUMN id SET DEFAULT nextval('public.project_aliases_id_seq'::regclass);


--
-- Name: projects id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects ALTER COLUMN id SET DEFAULT nextval('public.projects_id_seq'::regclass);


--
-- Name: session_items id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_items ALTER COLUMN id SET DEFAULT nextval('public.session_items_id_seq'::regclass);


--
-- Name: sessions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions ALTER COLUMN id SET DEFAULT nextval('public.sessions_id_seq'::regclass);


--
-- Name: specifications id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.specifications ALTER COLUMN id SET DEFAULT nextval('public.specifications_id_seq'::regclass);


--
-- Name: workflows id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflows ALTER COLUMN id SET DEFAULT nextval('public.workflows_id_seq'::regclass);


--
-- Name: agent_specs agent_specs_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_specs
    ADD CONSTRAINT agent_specs_name_key UNIQUE (name);


--
-- Name: agent_specs agent_specs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_specs
    ADD CONSTRAINT agent_specs_pkey PRIMARY KEY (id);


--
-- Name: annotations annotations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.annotations
    ADD CONSTRAINT annotations_pkey PRIMARY KEY (id);


--
-- Name: api_keys api_keys_api_key_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_api_key_hash_key UNIQUE (api_key_hash);


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);


--
-- Name: approaches approaches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approaches
    ADD CONSTRAINT approaches_pkey PRIMARY KEY (id);


--
-- Name: backlog_analysis backlog_analysis_batch_run_id_lesson_a_id_lesson_b_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backlog_analysis
    ADD CONSTRAINT backlog_analysis_batch_run_id_lesson_a_id_lesson_b_id_key UNIQUE (batch_run_id, lesson_a_id, lesson_b_id);


--
-- Name: backlog_analysis backlog_analysis_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backlog_analysis
    ADD CONSTRAINT backlog_analysis_pkey PRIMARY KEY (id);


--
-- Name: consolidation_queue consolidation_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consolidation_queue
    ADD CONSTRAINT consolidation_queue_pkey PRIMARY KEY (id);


--
-- Name: containers containers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.containers
    ADD CONSTRAINT containers_pkey PRIMARY KEY (id);


--
-- Name: databases databases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.databases
    ADD CONSTRAINT databases_pkey PRIMARY KEY (id);


--
-- Name: guardrails guardrails_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guardrails
    ADD CONSTRAINT guardrails_pkey PRIMARY KEY (id);


--
-- Name: journal journal_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal
    ADD CONSTRAINT journal_pkey PRIMARY KEY (id);


--
-- Name: key_files key_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.key_files
    ADD CONSTRAINT key_files_pkey PRIMARY KEY (id);


--
-- Name: lesson_conflicts lesson_conflicts_lesson_a_id_lesson_b_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_conflicts
    ADD CONSTRAINT lesson_conflicts_lesson_a_id_lesson_b_id_key UNIQUE (lesson_a_id, lesson_b_id);


--
-- Name: lesson_conflicts lesson_conflicts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_conflicts
    ADD CONSTRAINT lesson_conflicts_pkey PRIMARY KEY (id);


--
-- Name: lesson_merges lesson_merges_canonical_id_merged_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_merges
    ADD CONSTRAINT lesson_merges_canonical_id_merged_id_key UNIQUE (canonical_id, merged_id);


--
-- Name: lesson_merges lesson_merges_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_merges
    ADD CONSTRAINT lesson_merges_pkey PRIMARY KEY (id);


--
-- Name: lessons lessons_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons
    ADD CONSTRAINT lessons_pkey PRIMARY KEY (id);


--
-- Name: machines machines_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machines
    ADD CONSTRAINT machines_name_key UNIQUE (name);


--
-- Name: machines machines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machines
    ADD CONSTRAINT machines_pkey PRIMARY KEY (id);


--
-- Name: mcp_server_projects mcp_server_projects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_server_projects
    ADD CONSTRAINT mcp_server_projects_pkey PRIMARY KEY (server_id, project_id);


--
-- Name: mcp_server_tools mcp_server_tools_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_server_tools
    ADD CONSTRAINT mcp_server_tools_pkey PRIMARY KEY (id);


--
-- Name: mcp_server_tools mcp_server_tools_server_id_tool_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_server_tools
    ADD CONSTRAINT mcp_server_tools_server_id_tool_name_key UNIQUE (server_id, tool_name);


--
-- Name: mcp_servers mcp_servers_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_name_key UNIQUE (name);


--
-- Name: mcp_servers mcp_servers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_pkey PRIMARY KEY (id);


--
-- Name: oauth_access_tokens oauth_access_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_access_tokens
    ADD CONSTRAINT oauth_access_tokens_pkey PRIMARY KEY (token);


--
-- Name: oauth_client_family oauth_client_family_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_client_family
    ADD CONSTRAINT oauth_client_family_pkey PRIMARY KEY (client_id);


--
-- Name: oauth_clients oauth_clients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_clients
    ADD CONSTRAINT oauth_clients_pkey PRIMARY KEY (client_id);


--
-- Name: oauth_refresh_tokens oauth_refresh_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_refresh_tokens
    ADD CONSTRAINT oauth_refresh_tokens_pkey PRIMARY KEY (token);


--
-- Name: patterns patterns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patterns
    ADD CONSTRAINT patterns_pkey PRIMARY KEY (id);


--
-- Name: permissions permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT permissions_pkey PRIMARY KEY (id);


--
-- Name: project_aliases project_aliases_alias_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_aliases
    ADD CONSTRAINT project_aliases_alias_key UNIQUE (alias);


--
-- Name: project_aliases project_aliases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_aliases
    ADD CONSTRAINT project_aliases_pkey PRIMARY KEY (id);


--
-- Name: project_state project_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_state
    ADD CONSTRAINT project_state_pkey PRIMARY KEY (project_id);


--
-- Name: projects projects_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_name_key UNIQUE (name);


--
-- Name: projects projects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);


--
-- Name: session_items session_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_items
    ADD CONSTRAINT session_items_pkey PRIMARY KEY (id);


--
-- Name: sessions sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id);


--
-- Name: specifications specifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.specifications
    ADD CONSTRAINT specifications_pkey PRIMARY KEY (id);


--
-- Name: workflows workflows_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflows
    ADD CONSTRAINT workflows_pkey PRIMARY KEY (id);


--
-- Name: idx_access_tokens_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_access_tokens_expires ON public.oauth_access_tokens USING btree (expires_at);


--
-- Name: idx_agent_specs_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_specs_embedding ON public.agent_specs USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');


--
-- Name: idx_agent_specs_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_specs_project ON public.agent_specs USING btree (project_id);


--
-- Name: idx_agent_specs_retired; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_specs_retired ON public.agent_specs USING btree (retired_at) WHERE (retired_at IS NULL);


--
-- Name: idx_agent_specs_triggers; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_specs_triggers ON public.agent_specs USING gin (triggers);


--
-- Name: idx_agent_specs_tsv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_specs_tsv ON public.agent_specs USING gin (tsv);


--
-- Name: idx_annotations_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_annotations_entity ON public.annotations USING btree (entity_type, entity_id);


--
-- Name: idx_api_keys_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_api_keys_active ON public.api_keys USING btree (revoked_at) WHERE (revoked_at IS NULL);


--
-- Name: idx_approaches_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_approaches_project ON public.approaches USING btree (project_id);


--
-- Name: idx_approaches_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_approaches_status ON public.approaches USING btree (status);


--
-- Name: idx_backlog_analysis_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_backlog_analysis_batch ON public.backlog_analysis USING btree (batch_run_id);


--
-- Name: idx_backlog_analysis_verdict; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_backlog_analysis_verdict ON public.backlog_analysis USING btree (verdict, confidence DESC);


--
-- Name: idx_consolidation_queue_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_consolidation_queue_pending ON public.consolidation_queue USING btree (enqueued_at) WHERE (decided_at IS NULL);


--
-- Name: idx_containers_machine; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_containers_machine ON public.containers USING btree (machine_id);


--
-- Name: idx_containers_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_containers_project ON public.containers USING btree (project);


--
-- Name: idx_guardrails_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_guardrails_project ON public.guardrails USING btree (project_id);


--
-- Name: idx_journal_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_journal_date ON public.journal USING btree (entry_date DESC);


--
-- Name: idx_journal_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_journal_embedding ON public.journal USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');


--
-- Name: idx_journal_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_journal_project ON public.journal USING btree (project_id);


--
-- Name: idx_journal_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_journal_tags ON public.journal USING gin (tags);


--
-- Name: idx_journal_tsv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_journal_tsv ON public.journal USING gin (tsv);


--
-- Name: idx_key_files_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_key_files_project ON public.key_files USING btree (project_id);


--
-- Name: idx_lesson_conflicts_unresolved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lesson_conflicts_unresolved ON public.lesson_conflicts USING btree (flagged_at) WHERE (resolved_at IS NULL);


--
-- Name: idx_lesson_merges_canonical; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lesson_merges_canonical ON public.lesson_merges USING btree (canonical_id) WHERE (reversed_at IS NULL);


--
-- Name: idx_lesson_merges_merged; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lesson_merges_merged ON public.lesson_merges USING btree (merged_id) WHERE (reversed_at IS NULL);


--
-- Name: idx_lessons_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_embedding ON public.lessons USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');


--
-- Name: idx_lessons_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_project ON public.lessons USING btree (project_id);


--
-- Name: idx_lessons_retired; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_retired ON public.lessons USING btree (retired_at) WHERE (retired_at IS NULL);


--
-- Name: idx_lessons_severity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_severity ON public.lessons USING btree (severity);


--
-- Name: idx_lessons_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_tags ON public.lessons USING gin (tags);


--
-- Name: idx_lessons_tsv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_tsv ON public.lessons USING gin (tsv);


--
-- Name: idx_mcp_server_tools_tsv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_server_tools_tsv ON public.mcp_server_tools USING gin (tsv);


--
-- Name: idx_mcp_servers_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_servers_embedding ON public.mcp_servers USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');


--
-- Name: idx_mcp_servers_machine; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_servers_machine ON public.mcp_servers USING btree (machine_id);


--
-- Name: idx_mcp_servers_retired; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_servers_retired ON public.mcp_servers USING btree (retired_at) WHERE (retired_at IS NULL);


--
-- Name: idx_mcp_servers_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_servers_status ON public.mcp_servers USING btree (status);


--
-- Name: idx_mcp_tools_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_tools_embedding ON public.mcp_server_tools USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');


--
-- Name: idx_mcp_tools_server; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_tools_server ON public.mcp_server_tools USING btree (server_id);


--
-- Name: idx_patterns_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_patterns_embedding ON public.patterns USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');


--
-- Name: idx_patterns_tsv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_patterns_tsv ON public.patterns USING gin (tsv);


--
-- Name: idx_permissions_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_permissions_scope ON public.permissions USING btree (scope);


--
-- Name: idx_project_aliases_alias; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_aliases_alias ON public.project_aliases USING btree (lower((alias)::text));


--
-- Name: idx_refresh_tokens_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_refresh_tokens_expires ON public.oauth_refresh_tokens USING btree (expires_at);


--
-- Name: idx_sessions_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sessions_embedding ON public.sessions USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');


--
-- Name: idx_sessions_machine; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sessions_machine ON public.sessions USING btree (machine_id);


--
-- Name: idx_sessions_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sessions_project ON public.sessions USING btree (project_id);


--
-- Name: idx_sessions_tsv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sessions_tsv ON public.sessions USING gin (tsv);


--
-- Name: idx_specifications_tsv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_specifications_tsv ON public.specifications USING gin (tsv);


--
-- Name: idx_specs_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_specs_embedding ON public.specifications USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');


--
-- Name: idx_specs_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_specs_project ON public.specifications USING btree (project_id);


--
-- Name: idx_specs_retired; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_specs_retired ON public.specifications USING btree (retired_at) WHERE (retired_at IS NULL);


--
-- Name: idx_specs_subsystem; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_specs_subsystem ON public.specifications USING btree (subsystem);


--
-- Name: idx_specs_triggers; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_specs_triggers ON public.specifications USING gin (triggers);


--
-- Name: agent_specs trg_agent_specs_tsv; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agent_specs_tsv BEFORE INSERT OR UPDATE ON public.agent_specs FOR EACH ROW EXECUTE FUNCTION public.agent_specs_tsv_trigger();


--
-- Name: journal trg_journal_tsv; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_journal_tsv BEFORE INSERT OR UPDATE ON public.journal FOR EACH ROW EXECUTE FUNCTION public.journal_tsv_trigger();


--
-- Name: lessons trg_lessons_tsv; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_lessons_tsv BEFORE INSERT OR UPDATE ON public.lessons FOR EACH ROW EXECUTE FUNCTION public.lessons_tsv_trigger();


--
-- Name: mcp_server_tools trg_mcp_server_tools_tsv; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_mcp_server_tools_tsv BEFORE INSERT OR UPDATE ON public.mcp_server_tools FOR EACH ROW EXECUTE FUNCTION public.mcp_server_tools_tsv_trigger();


--
-- Name: patterns trg_patterns_tsv; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_patterns_tsv BEFORE INSERT OR UPDATE ON public.patterns FOR EACH ROW EXECUTE FUNCTION public.patterns_tsv_trigger();


--
-- Name: sessions trg_sessions_tsv; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_sessions_tsv BEFORE INSERT OR UPDATE ON public.sessions FOR EACH ROW EXECUTE FUNCTION public.sessions_tsv_trigger();


--
-- Name: specifications trg_specifications_tsv; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_specifications_tsv BEFORE INSERT OR UPDATE ON public.specifications FOR EACH ROW EXECUTE FUNCTION public.specifications_tsv_trigger();


--
-- Name: machines update_machines_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_machines_updated_at BEFORE UPDATE ON public.machines FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: project_state update_project_state_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_project_state_updated_at BEFORE UPDATE ON public.project_state FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: projects update_projects_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_projects_updated_at BEFORE UPDATE ON public.projects FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: agent_specs agent_specs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_specs
    ADD CONSTRAINT agent_specs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


--
-- Name: approaches approaches_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approaches
    ADD CONSTRAINT approaches_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: backlog_analysis backlog_analysis_lesson_a_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backlog_analysis
    ADD CONSTRAINT backlog_analysis_lesson_a_id_fkey FOREIGN KEY (lesson_a_id) REFERENCES public.lessons(id) ON DELETE CASCADE;


--
-- Name: backlog_analysis backlog_analysis_lesson_b_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backlog_analysis
    ADD CONSTRAINT backlog_analysis_lesson_b_id_fkey FOREIGN KEY (lesson_b_id) REFERENCES public.lessons(id) ON DELETE CASCADE;


--
-- Name: consolidation_queue consolidation_queue_candidate_lesson_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consolidation_queue
    ADD CONSTRAINT consolidation_queue_candidate_lesson_id_fkey FOREIGN KEY (candidate_lesson_id) REFERENCES public.lessons(id) ON DELETE CASCADE;


--
-- Name: consolidation_queue consolidation_queue_new_lesson_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consolidation_queue
    ADD CONSTRAINT consolidation_queue_new_lesson_id_fkey FOREIGN KEY (new_lesson_id) REFERENCES public.lessons(id) ON DELETE CASCADE;


--
-- Name: containers containers_machine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.containers
    ADD CONSTRAINT containers_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id) ON DELETE CASCADE;


--
-- Name: databases databases_machine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.databases
    ADD CONSTRAINT databases_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id) ON DELETE CASCADE;


--
-- Name: guardrails guardrails_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guardrails
    ADD CONSTRAINT guardrails_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: journal journal_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal
    ADD CONSTRAINT journal_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


--
-- Name: journal journal_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal
    ADD CONSTRAINT journal_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE SET NULL;


--
-- Name: key_files key_files_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.key_files
    ADD CONSTRAINT key_files_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: lesson_conflicts lesson_conflicts_lesson_a_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_conflicts
    ADD CONSTRAINT lesson_conflicts_lesson_a_id_fkey FOREIGN KEY (lesson_a_id) REFERENCES public.lessons(id) ON DELETE CASCADE;


--
-- Name: lesson_conflicts lesson_conflicts_lesson_b_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_conflicts
    ADD CONSTRAINT lesson_conflicts_lesson_b_id_fkey FOREIGN KEY (lesson_b_id) REFERENCES public.lessons(id) ON DELETE CASCADE;


--
-- Name: lesson_merges lesson_merges_canonical_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_merges
    ADD CONSTRAINT lesson_merges_canonical_id_fkey FOREIGN KEY (canonical_id) REFERENCES public.lessons(id) ON DELETE CASCADE;


--
-- Name: lesson_merges lesson_merges_merged_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_merges
    ADD CONSTRAINT lesson_merges_merged_id_fkey FOREIGN KEY (merged_id) REFERENCES public.lessons(id) ON DELETE CASCADE;


--
-- Name: lessons lessons_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons
    ADD CONSTRAINT lessons_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


--
-- Name: mcp_server_projects mcp_server_projects_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_server_projects
    ADD CONSTRAINT mcp_server_projects_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: mcp_server_projects mcp_server_projects_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_server_projects
    ADD CONSTRAINT mcp_server_projects_server_id_fkey FOREIGN KEY (server_id) REFERENCES public.mcp_servers(id) ON DELETE CASCADE;


--
-- Name: mcp_server_tools mcp_server_tools_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_server_tools
    ADD CONSTRAINT mcp_server_tools_server_id_fkey FOREIGN KEY (server_id) REFERENCES public.mcp_servers(id) ON DELETE CASCADE;


--
-- Name: mcp_servers mcp_servers_machine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id) ON DELETE SET NULL;


--
-- Name: oauth_access_tokens oauth_access_tokens_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_access_tokens
    ADD CONSTRAINT oauth_access_tokens_client_id_fkey FOREIGN KEY (client_id) REFERENCES public.oauth_clients(client_id) ON DELETE CASCADE;


--
-- Name: oauth_client_family oauth_client_family_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_client_family
    ADD CONSTRAINT oauth_client_family_client_id_fkey FOREIGN KEY (client_id) REFERENCES public.oauth_clients(client_id) ON DELETE CASCADE;


--
-- Name: oauth_refresh_tokens oauth_refresh_tokens_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_refresh_tokens
    ADD CONSTRAINT oauth_refresh_tokens_client_id_fkey FOREIGN KEY (client_id) REFERENCES public.oauth_clients(client_id) ON DELETE CASCADE;


--
-- Name: project_aliases project_aliases_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_aliases
    ADD CONSTRAINT project_aliases_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_state project_state_last_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_state
    ADD CONSTRAINT project_state_last_session_id_fkey FOREIGN KEY (last_session_id) REFERENCES public.sessions(id) ON DELETE SET NULL;


--
-- Name: project_state project_state_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_state
    ADD CONSTRAINT project_state_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: projects projects_machine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id) ON DELETE SET NULL;


--
-- Name: session_items session_items_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_items
    ADD CONSTRAINT session_items_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;


--
-- Name: sessions sessions_machine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id) ON DELETE SET NULL;


--
-- Name: sessions sessions_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


--
-- Name: specifications specifications_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.specifications
    ADD CONSTRAINT specifications_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: workflows workflows_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflows
    ADD CONSTRAINT workflows_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


--
-- PostgreSQL database dump complete
--


