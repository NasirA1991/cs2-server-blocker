# bash completion for cs2blockctl
_cs2blockctl_pops() {
    local line code
    while IFS= read -r line; do
        # "  ams   Amsterdam ...": read skips leading blanks; region headers
        # start with a capital letter and are filtered out.
        read -r code _ <<< "$line"
        [[ $code =~ ^[a-z0-9]+$ ]] && printf '%s\n' "$code"
    done < <(cs2blockctl list 2>/dev/null)
}

_cs2blockctl() {
    local cur prev cmd
    COMPREPLY=()
    cur=${COMP_WORDS[COMP_CWORD]}
    prev=${COMP_WORDS[COMP_CWORD-1]}
    cmd=${COMP_WORDS[1]:-}

    if (( COMP_CWORD == 1 )); then
        COMPREPLY=( $(compgen -W "--sudo --version list status block unblock only preset refresh apply clear pause resume ping counters doctor persistence version" -- "$cur") )
        return
    fi

    case $cmd in
        list)
            COMPREPLY=( $(compgen -W "--region --json" -- "$cur") ) ;;
        status)
            COMPREPLY=( $(compgen -W "--json" -- "$cur") ) ;;
        block|unblock|only|ping)
            if [[ $prev == --region ]]; then
                COMPREPLY=( $(compgen -W "Europe 'North America' 'South America' Asia Oceania Africa 'Middle East'" -- "$cur") )
            elif [[ $cur == -* ]]; then
                COMPREPLY=( $(compgen -W "--region --json --timeout --force" -- "$cur") )
            else
                COMPREPLY=( $(compgen -W "$(_cs2blockctl_pops)" -- "$cur") )
            fi ;;
        preset)
            COMPREPLY=( $(compgen -W "--list --save --delete" -- "$cur") ) ;;
        counters)
            COMPREPLY=( $(compgen -W "--json --watch --interval" -- "$cur") ) ;;
        doctor)
            COMPREPLY=( $(compgen -W "--json --privileged" -- "$cur") ) ;;
        persistence)
            COMPREPLY=( $(compgen -W "enable disable" -- "$cur") ) ;;
    esac
}
complete -F _cs2blockctl cs2blockctl
