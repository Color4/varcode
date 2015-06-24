# Copyright (c) 2015. Mount Sinai School of Medicine
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# required so that 'import vcf' gets the global PyVCF package,
# rather than our local vcf module
from __future__ import absolute_import

import requests
import zlib
import collections

try:
    from urlparse import urlparse  # Python 2
except ImportError:
    from urllib.parse import urlparse  # Python 3

import pandas
from pyensembl import cached_release
import typechecks
import vcf  # PyVCF

from .reference_name import (
    infer_reference_name,
    ensembl_release_number_for_reference_name
)
from .variant import Variant
from .variant_collection import VariantCollection

def load_vcf(
        path,
        only_passing=True,
        ensembl_version=None,
        reference_name=None,
        reference_vcf_key="reference",
        allow_extended_nucleotides=False,
        max_variants=None):
    """
    Load reference name and Variant objects from the given VCF filename.

    This uses PyVCF to parse the file. It is slower than the pandas
    implementation used in `load_vcf_fast`, but is better tested and more 
    robust.

    Parameters
    ----------

    path : str or vcf.Reader
        Path or URL to VCF (*.vcf) or compressed VCF (*.vcf.gz). Supported URL 
        schemes are "file", "http", "https", and "ftp". Can also be a pyvcf
        Reader instance.

    only_passing : boolean, optional
        If true, any entries whose FILTER field is not one of "." or "PASS" is
        dropped.

    ensembl_version : int, optional
        Which release of Ensembl to use for annotation, by default inferred
        from the reference path. If specified, then `reference_name` and
        `reference_vcf_key` are ignored.

    reference_name : str, optional
        Name of reference genome against which variants from VCF were aligned.
        If specified, then `reference_vcf_key` is ignored.

    reference_vcf_key : str, optional
        Name of metadata field which contains path to reference FASTA
        file (default = 'reference')

    allow_extended_nucleotides : boolean, default False
        Allow characters other that A,C,T,G in the ref and alt strings.

    max_variants : int, optional
        If specified, return only the first max_variants variants.
    """

    variants = []
    metadata = {}
    handle = PyVCFReaderFromPathOrURL(path)
    try:
        ensembl = make_ensembl(
            handle.vcf_reader,
            ensembl_version,
            reference_name,
            reference_vcf_key)

        for record in handle.vcf_reader:
            if only_passing and record.FILTER and record.FILTER != "PASS":
                continue
            for (alt_num, alt) in enumerate(record.ALT):
                # We ignore "no-call" variants, i.e. those where X.ALT = [None]
                if not alt:
                    continue
                variant = Variant(
                    contig=record.CHROM,
                    start=record.POS,
                    ref=record.REF,
                    alt=alt.sequence,
                    ensembl=ensembl,
                    allow_extended_nucleotides=allow_extended_nucleotides)
                variants.append(variant)
                metadata[variant] = {
                    "id": record.ID,
                    "qual": record.QUAL,
                    "filter": record.FILTER,
                    "alt_allele_index": alt_num,
                    "info": dict(record.INFO),
                }
                if max_variants and len(variants) > max_variants:
                    raise StopIteration
    except StopIteration:
        pass
    finally:
        handle.close()

    return VariantCollection(
        variants=variants, path=handle.path, metadata=metadata)
    
def load_vcf_fast(
        path,
        only_passing=True,
        ensembl_version=None,
        reference_name=None,
        reference_vcf_key="reference",
        allow_extended_nucleotides=False,
        include_info=True,
        chunk_size=10**5,
        max_variants=None):
    '''
    Load reference name and Variant objects from the given VCF filename.

    This is an experimental faster implementation of `load_vcf`. It is
    typically about 2X faster, and with `include_info=False`, about 4X faster.

    Currently only local files are supported by this function (no http). If you
    call this on an HTTP URL, it will fall back to `load_vcf`.

    Parameters
    ----------

    path : str
        Path to VCF (*.vcf) or compressed VCF (*.vcf.gz).

    only_passing : boolean, optional
        If true, any entries whose FILTER field is not one of "." or "PASS" is
        dropped.

    ensembl_version : int, optional
        Which release of Ensembl to use for annotation, by default inferred
        from the reference path. If specified, then `reference_name` and
        `reference_vcf_key` are ignored.

    reference_name : str, optional
        Name of reference genome against which variants from VCF were aligned.
        If specified, then `reference_vcf_key` is ignored.

    reference_vcf_key : str, optional
        Name of metadata field which contains path to reference FASTA
        file (default = 'reference')

    allow_extended_nucleotides : boolean, default False
        Allow characters other that A,C,T,G in the ref and alt strings.

    include_info : boolean, default True
        Whether to parse the info column. If you don't need that column, set to
        False for faster parsing.

    chunk_size: int, optional
        Number of records to load in memory at once.

    max_variants : int, optional
        If specified, return only the first max_variants variants.
    '''

    typechecks.require_string(path, "Path or URL to VCF")
    parsed_path = urlparse(path)

    if parsed_path.scheme and parsed_path.scheme.lower() != "file":
        # pandas.read_table nominally supports HTTP, but it tends to crash on
        # large files and does not support gzip. Switching to the python-based
        # implementation of read_table (with engine="python") helps with some
        # issues but introduces a new set of problems (e.g. the dtype parameter
        # is not accepted). For these reasons, we're currently not attempting
        # to load VCFs over HTTP with pandas, and fall back to the pyvcf
        # implementation here.
        return load_vcf(
            path,
            only_passing=only_passing,
            ensembl_version=ensembl_version,
            reference_name=reference_name,
            reference_vcf_key=reference_vcf_key,
            allow_extended_nucleotides=allow_extended_nucleotides,
            max_variants=max_variants)

    # Loading a local file.
    # The file will be opened twice: first to parse the header with pyvcf, then
    # by pandas to read the data.

    # PyVCF reads the metadata immediately and stops at the first line with
    # data. We can close the file after that.
    handle = PyVCFReaderFromPathOrURL(path)
    handle.close()

    ensembl = make_ensembl(
        handle.vcf_reader, ensembl_version, reference_name, reference_vcf_key)

    df_iterator = read_vcf_into_dataframe(
        path, include_info=include_info, chunk_size=chunk_size)

    return dataframes_to_variant_collection(
        df_iterator,
        info_parser=handle.vcf_reader._parse_info if include_info else None,
        only_passing=only_passing,
        max_variants=max_variants,
        variant_kwargs={
            'ensembl': ensembl,
            'allow_extended_nucleotides': allow_extended_nucleotides},
        variant_collection_kwargs={"path": path})

def dataframes_to_variant_collection(
        dataframes,
        info_parser=None,
        only_passing=True,
        max_variants=None,
        variant_kwargs={},
        variant_collection_kwargs={}):
    '''
    Load a VariantCollection from an iterable of pandas dataframes.

    This takes an iterable of dataframes instead of a single dataframe to avoid
    having to load huge dataframes at once into memory. If you have a single
    dataframe, just pass it in a single-element list.

    Parameters
    ----------
    dataframes
        Iterable of dataframes (e.g. a generator). Expected columns are:
            ["CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER"]
        and 'INFO' if `info_parser` is not Null. Columns must be in this
        order.

    info_parser : string -> object, optional
        Callable to parse INFO strings.

    only_passing : boolean, optional
        If true, any entries whose FILTER field is not one of "." or "PASS" is
        dropped.

    max_variants : int, optional
        If specified, return only the first max_variants variants.

    variant_kwargs : dict, optional
        Additional keyword paramters to pass to Variant.__init__

    variant_collection_kwargs : dict, optional
        Additional keyword parameters to pass to VariantCollection.__init__.
    '''

    expected_columns = (
        ["CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER"] +
        (["INFO"] if info_parser else []))

    if info_parser:
        def records(chunk):
            for tpl in chunk.itertuples():
                # Parse the info field
                info = info_parser(tpl[-1])
                yield tpl[:-1] + (info,)
    else:
        def records(chunk):
            for tpl in chunk.itertuples():
                # Use None as the info field.
                yield tpl + (None,)

    variants = []
    metadata = {}
    try:
        for chunk in dataframes:
            if not info_parser and 'INFO' in chunk:
                del chunk['INFO']

            assert chunk.columns.tolist() == expected_columns,\
                "dataframe columns (%s) do not match expected columns (%s)" % (
                    chunk.columns, expected_columns)
                
            iterator = records(chunk)
            for (i, chrom, pos, id_, ref, alts, qual, flter, info) in iterator:
                if flter == ".":
                    flter = None
                elif flter == "PASS":
                    flter = []
                elif only_passing:
                    continue
                else:
                    flter = flter.split(';')
                if id_ == ".":
                    id_ = None
                qual = float(qual) if qual != "." else None
                alt_num = 0
                for alt in alts.split(","):
                    if alt != ".":
                        variant = Variant(
                            chrom,
                            int(pos),  # want a Python int not numpy.int64
                            ref,
                            alt,
                            **variant_kwargs)
                        variants.append(variant)
                        metadata[variant] = {
                            'id': id_,
                            'qual': qual,
                            'filter': flter,
                            'info': info,
                            'alt_allele_index': alt_num, 
                        }
                        if max_variants and len(variants) > max_variants:
                            raise StopIteration
                    alt_num += 1
    except StopIteration:
        pass

    return VariantCollection(
        variants=variants, metadata=metadata, **variant_collection_kwargs)

def read_vcf_into_dataframe(path, include_info=False, chunk_size=None):
    """
    Load the data of a VCF into a pandas dataframe. All headers are ignored.

    Parameters
    ----------
    path : str
        Path to local file. HTTP and other protocols are not implemented.

    include_info : boolean, default False
        If true, the INFO field is not parsed, but is included as a string in
        the resulting data frame. If false, the INFO field is omitted.

    chunk_size : int, optional
        If buffering is desired, the number of rows per chunk.

    Returns
    ---------
    If chunk_size is None (the default), a dataframe with the contents of the
    VCF file. Otherwise, an iterable of dataframes, each with chunk_size rows.

    """
    vcf_field_types = collections.OrderedDict()
    vcf_field_types['CHROM'] = str
    vcf_field_types['POS'] = int
    vcf_field_types['ID'] = str
    vcf_field_types['REF'] = str
    vcf_field_types['ALT'] = str
    vcf_field_types['QUAL'] = str
    vcf_field_types['FILTER'] = str
    if include_info:
        vcf_field_types['INFO'] = str

    parsed_path = urlparse(path)
    if not parsed_path.scheme or parsed_path.scheme.lower() == "file":
        path = parsed_path.path
    else:
        raise NotImplementedError("Only local files are supported.")

    reader = pandas.read_table(
        path,
        compression='infer',
        comment="#",
        chunksize=chunk_size,
        dtype=vcf_field_types,
        names=list(vcf_field_types),
        usecols=range(len(vcf_field_types)))
    return reader

class PyVCFReaderFromPathOrURL(object):
    '''
    Thin wrapper over a PyVCF Reader object that supports loading over URLs,
    and a close() function (pyvcf somehow doesn't have a close() funciton).

    Attributes
    ----------
    path : string or None
        path that was loaded, if available.

    vcf_reader : pyvcf Reader instance
    '''
    def __init__(self, path):
        """
        Construct a new wrapper.

        Parameters
        ----------
        path : string or pyvcf Reader instance
            Path or URL to load, or Reader instance.
        """
        self.path = None  # string path, if available.
        self.vcf_reader = None  # vcf_reader. Will always be set.
        self._to_close = None  # object to call close() on when we're done.

        if isinstance(path, vcf.Reader):
            self.vcf_reader = path
            return

        typechecks.require_string(path, "Path or URL to VCF")
        self.path = path
        parsed_path = urlparse(path)
        if not parsed_path.scheme or parsed_path.scheme.lower() == 'file':
            self.vcf_reader = vcf.Reader(filename=parsed_path.path)
        elif parsed_path.scheme.lower() in ("http", "https", "ftp"):
            self._to_close = response = requests.get(path, stream=True)
            response.raise_for_status()  # raise error on 404, etc.
            if path.endswith(".gz"):
                lines = stream_gzip_decompress_lines(response.iter_content())
            else:
                lines = response.iter_lines(decode_unicode=True)
            self.vcf_reader = vcf.Reader(fsock=lines, compressed=False)
        else:
            raise ValueError("Unsupported scheme: %s" % parsed_path.scheme)

    def close(self):
        if self._to_close is not None:
            self._to_close.close()

def stream_gzip_decompress_lines(stream):
    """
    Uncompress a gzip stream into lines of text.

    Parameters
    ----------
    Generator of chunks of gzip compressed text.

    Returns
    -------
    Generator of uncompressed lines.
    """
    dec = zlib.decompressobj(zlib.MAX_WBITS | 16)
    previous = ""
    for compressed_chunk in stream:
        chunk = dec.decompress(compressed_chunk).decode()
        if chunk:
            lines = (previous + chunk).split("\n")
            previous = lines.pop()
            for line in lines:
                yield line
    yield previous

def make_ensembl(
        vcf_reader,
        ensembl_version,
        reference_name, 
        reference_vcf_key):
    '''
    Helper function to make an ensembl instance.
    '''
    
    if not ensembl_version:
        if reference_name:
            # normalize the reference name in case it's in a weird format
            reference_name = infer_reference_name(reference_name)
        elif reference_vcf_key not in vcf_reader.metadata:
            raise ValueError("Unable to infer reference genome for %s" % (
                vcf_reader.filename,))
        else:
            reference_path = vcf_reader.metadata[reference_vcf_key]
            reference_name = infer_reference_name(reference_path)
        ensembl_version = ensembl_release_number_for_reference_name(
            reference_name)
    return cached_release(ensembl_version)
