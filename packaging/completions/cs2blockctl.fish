# fish completion for cs2blockctl
set -l commands list status block unblock only preset refresh apply clear pause resume ping counters doctor persistence version

complete -c cs2blockctl -f
complete -c cs2blockctl -l sudo -d 'Use sudo instead of pkexec'
complete -c cs2blockctl -l version -d 'Print version'
complete -c cs2blockctl -n "not __fish_seen_subcommand_from $commands" -a "$commands"

for cmd in block unblock only ping
    complete -c cs2blockctl -n "__fish_seen_subcommand_from $cmd" -a "(cs2blockctl list 2>/dev/null | awk '/^  [a-z0-9]+/ {print \$1}')"
end

complete -c cs2blockctl -n "__fish_seen_subcommand_from list" -l region -d 'Filter by region'
complete -c cs2blockctl -n "__fish_seen_subcommand_from list" -l json -d 'JSON output'
complete -c cs2blockctl -n "__fish_seen_subcommand_from status" -l json -d 'JSON output'
complete -c cs2blockctl -n "__fish_seen_subcommand_from ping" -l region -d 'Filter by region'
complete -c cs2blockctl -n "__fish_seen_subcommand_from ping" -l timeout -d 'Timeout in seconds'
complete -c cs2blockctl -n "__fish_seen_subcommand_from ping" -l json -d 'JSON output'
complete -c cs2blockctl -n "__fish_seen_subcommand_from preset" -l list -d 'List presets'
complete -c cs2blockctl -n "__fish_seen_subcommand_from preset" -l save -d 'Save a preset'
complete -c cs2blockctl -n "__fish_seen_subcommand_from preset" -l delete -d 'Delete a preset'
complete -c cs2blockctl -n "__fish_seen_subcommand_from counters" -l json -d 'JSON output'
complete -c cs2blockctl -n "__fish_seen_subcommand_from counters" -l watch -d 'Follow counters'
complete -c cs2blockctl -n "__fish_seen_subcommand_from counters" -l interval -d 'Seconds between samples'
complete -c cs2blockctl -n "__fish_seen_subcommand_from doctor" -l json -d 'JSON output'
complete -c cs2blockctl -n "__fish_seen_subcommand_from doctor" -l privileged -d 'Run root checks'
complete -c cs2blockctl -n "__fish_seen_subcommand_from persistence" -a 'enable disable'
